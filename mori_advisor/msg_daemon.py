"""mori-msg daemon — long-running NATS pull consumer.

Subscribes to mori.msg.<hostname> + mori.msg.broadcast + mori.reply.*
on the MORI_MSG JetStream stream. Dispatches by message type:

  decision  → persist + write to memory_store immediately
  task      → persist (pending) + auto-ack back to sender
  done      → update status (dream runs on its own schedule)
  question / broadcast → persist (pending) for /brief pickup
  ack / reply → update status of referenced message

Run:  python -m mori_advisor.msg_daemon
"""

import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
from pathlib import Path

import nats
from nats.errors import TimeoutError as JsTimeout
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy
from nats.js.errors import NotFoundError

from mori_advisor.provenance import Provenance

from .memory_store import MemoryStore
from .msg import (
    STREAM_NAME,
    MoriMessage,
    MsgType,
    _ensure_stream,
    build_message,
    publish_message,
)
from .msg_store import MsgStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [msg-daemon] %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("msg_daemon")

DATA_DIR = Path(os.environ.get("MORI_ADVISOR_DATA", "/data/mori-advisor"))
NATS_URL = os.environ.get("MORI_NATS_URL", "nats://localhost:4222")
HEADLESS_ENABLED = os.environ.get("MORI_MSG_HEADLESS_ENABLED", "false").lower() == "true"
HEADLESS_TRUSTED = {
    h.strip() for h in os.environ.get("MORI_MSG_HEADLESS_TRUSTED", "").split(",") if h.strip()
}

HOSTNAME = socket.gethostname().split(".")[0]
CONSUMER_NAME = f"mori-msg-{HOSTNAME}"

# Subjects this daemon cares about
FILTER_SUBJECTS = [
    f"mori.msg.{HOSTNAME}",
    "mori.msg.broadcast",
    "mori.reply.*",  # all reply threads — needed to update status
]


def _msg_addressed_to_me(raw_subject: str) -> bool:
    return raw_subject in (f"mori.msg.{HOSTNAME}", "mori.msg.broadcast")


def _is_reply_subject(raw_subject: str) -> bool:
    return raw_subject.startswith("mori.reply.")


async def _dispatch(
    msg_data: dict,
    raw_subject: str,
    msg_store: MsgStore,
    memory_store: MemoryStore,
) -> None:
    try:
        m = MoriMessage.model_validate({**msg_data, "from": msg_data.get("from", "")})
    except Exception as e:
        logger.warning("Failed to parse message: %s — %s", msg_data, e)
        return

    msg_type: MsgType = m.type

    if _is_reply_subject(raw_subject):
        # Reply/ack/done — update the referenced message's status
        if m.reply_to and msg_type in ("ack", "done", "reply"):
            msg_store.upsert(m, status=msg_type)
            msg_store.set_status(m.reply_to, msg_type)
            logger.info("Status update: %s → %s", m.reply_to[:8], msg_type)
        return

    if not _msg_addressed_to_me(raw_subject):
        return

    if msg_type == "decision":
        msg_store.upsert(m, status="ignored")
        memory_store.write(
            title=m.body[:80],
            body=m.body,
            type="project",
            tier="working",
            client=m.from_agent,
            origin_clients=[m.from_agent],
            provenance=Provenance(
                actor="msg", actor_detail=m.from_agent, source="msg_daemon", op="write"
            ),
        )
        logger.info("Decision from %s written to memory_store", m.from_agent)

    elif msg_type == "task":
        msg_store.upsert(m, status="pending")
        ack = build_message(
            to=m.from_agent,
            type="ack",
            body=f"Task received by {HOSTNAME}",
            reply_to=m.id,
        )
        try:
            await publish_message(NATS_URL, ack)
            logger.info("Task from %s persisted + acked (id=%s)", m.from_agent, m.id[:8])
        except Exception as e:
            logger.warning("Failed to send ack for task %s: %s", m.id[:8], e)

        if HEADLESS_ENABLED and m.from_agent in HEADLESS_TRUSTED:
            _spawn_headless(m.body)

    elif msg_type == "done":
        msg_store.upsert(m, status="done")
        if m.reply_to:
            msg_store.set_status(m.reply_to, "done")
        logger.info("Task done: %s", m.reply_to[:8] if m.reply_to else m.id[:8])

    elif msg_type in ("question", "broadcast"):
        msg_store.upsert(m, status="pending")
        logger.info("%s from %s persisted (id=%s)", msg_type, m.from_agent, m.id[:8])

    elif msg_type in ("ack", "reply"):
        msg_store.upsert(m, status=msg_type)
        if m.reply_to:
            msg_store.set_status(m.reply_to, msg_type)
        logger.info("Reply %s from %s", msg_type, m.from_agent)


def _spawn_headless(task_body: str) -> None:
    try:
        subprocess.Popen(
            ["claude", "--print", task_body],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("Spawned headless CC for task")
    except FileNotFoundError:
        logger.warning("Headless CC requested but 'claude' not found in PATH")


async def _run_once(msg_store: MsgStore, memory_store: MemoryStore) -> None:
    """Connect, consume, loop until disconnect."""
    nc = await nats.connect(NATS_URL)
    logger.info("Connected to NATS at %s", NATS_URL.split("@")[-1])
    js = nc.jetstream()
    await _ensure_stream(js)

    # Durable pull consumer — resumes from last acked position after restart.
    # DeliverPolicy.NEW on first creation; subsequent connects resume automatically.
    try:
        await js.consumer_info(STREAM_NAME, CONSUMER_NAME)
        logger.info("Resuming durable consumer %s", CONSUMER_NAME)
    except NotFoundError:
        await js.add_consumer(
            STREAM_NAME,
            ConsumerConfig(
                durable_name=CONSUMER_NAME,
                filter_subjects=FILTER_SUBJECTS,
                deliver_policy=DeliverPolicy.NEW,
                ack_policy=AckPolicy.EXPLICIT,
                max_deliver=5,
            ),
        )
        logger.info("Created durable consumer %s", CONSUMER_NAME)

    psub = await js.pull_subscribe(
        "",  # subject handled by consumer filter_subjects
        durable=CONSUMER_NAME,
        stream=STREAM_NAME,
    )

    logger.info(
        "Listening on mori.msg.%s + mori.msg.broadcast + mori.reply.*",
        HOSTNAME,
    )

    try:
        while True:
            try:
                batch = await psub.fetch(10, timeout=5)
            except (asyncio.TimeoutError, JsTimeout):
                continue

            for raw in batch:
                try:
                    msg_data = json.loads(raw.data.decode())
                    await _dispatch(
                        msg_data,
                        raw.subject,
                        msg_store,
                        memory_store,
                    )
                    await raw.ack()
                except Exception as e:
                    logger.warning("Error processing message on %s: %s", raw.subject, e)
                    await raw.ack()  # ack anyway — bad data should not block the queue
    finally:
        await nc.drain()


async def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    msg_store = MsgStore(db_path=DATA_DIR / "msg.db")
    memory_store = MemoryStore(db_path=DATA_DIR / "memories.db")

    backoff = 1.0
    while True:
        try:
            await _run_once(msg_store, memory_store)
        except Exception as e:
            logger.warning("NATS connection lost (%s) — reconnecting in %.0fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
        else:
            backoff = 1.0


if __name__ == "__main__":
    asyncio.run(main())

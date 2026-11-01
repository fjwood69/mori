"""mori-msg daemon — long-running NATS pull consumer.

Subscribes to mori.msg.<hostname> + mori.msg.broadcast + mori.reply.*
on the MORI_MSG JetStream stream. Dispatches by message type:

  decision  → persist + queue for HUMAN REVIEW (pending_writes, named from the
              message id; idempotent per id; capped; fails closed on a store
              error); nothing reaches memories until a reviewer approves. The
              sender is self-asserted: NATS uses shared credentials.
  task      → persist (pending) + auto-ack back to sender
  done      → update status (dream runs on its own schedule)
  question / broadcast → persist (pending) for /brief pickup
  ack / reply → update status of referenced message

Run:  python -m mori_advisor.msg_daemon
"""

import asyncio
import inspect
import json
import logging
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import nats
from nats.errors import TimeoutError as JsTimeout
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy
from nats.js.errors import NotFoundError

from mori_advisor.memory_store import _clean
from mori_advisor.names import normalise_name

from .msg import (
    STREAM_NAME,
    MoriMessage,
    MsgType,
    _ensure_stream,
    build_message,
    publish_message,
)
from .msg_store import MsgStore
from .store import get_store
from .store.base import BaseStore

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
MAX_PENDING_PER_SENDER = 20  # B1: review-queue flood guard
# B1 (code consult 3c61eeb7/fdec15f7): the sender name is self-asserted (NATS uses shared credentials), so a
# per-sender cap alone is bypassed by rotating names. A total cap bounds the review queue whatever the names.
MAX_PENDING_DECISIONS_TOTAL = 100

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


async def _maybe_await(x: Any) -> Any:
    return await x if inspect.isawaitable(x) else x


def _record(msg_store: MsgStore, m: MoriMessage, status: str) -> None:
    """Log the message with `status`, overwriting an earlier ``queue-error`` (upsert alone is INSERT OR IGNORE)."""
    msg_store.upsert(m, status=status)
    msg_store.set_status(m.id, status)


async def _queue_decision_for_review(
    m: MoriMessage, msg_store: MsgStore, memory_store: BaseStore
) -> None:
    """B1 (2026-11-01): a ``decision`` message is an agent's assertion, so it goes to HUMAN REVIEW
    (pending_writes), never straight into memories.

    * Named from the MESSAGE ID (``decision-msg-<id>``), never a slug of the title: a slug could collide with an
      existing memory and, on approval, overwrite it (board requirement 1). An existing memory of that name refuses.
    * Idempotent per message id: a re-delivered id that is already logged is skipped, so it can never REPLACE a
      pending row a reviewer is looking at. Only an earlier ``queue-error`` is retried.
    * ``proposed_by="msg:<host>"``, ``source="msg_daemon"``; the raw provenance (msg id, sender, recipient, ts,
      ``sender_verified: false``) is stored on the row for the reviewer (requirement 3). Approval keeps actor
      ``msg`` + host (requirement 2, in the store's approve()). The sender is NOT authenticated: NATS uses shared
      credentials, so ``from`` is whatever the publisher claims.
    * At most MAX_PENDING_PER_SENDER pending decisions per sender and MAX_PENDING_DECISIONS_TOTAL in all; beyond
      either the message is recorded ``rejected-queue-full`` (review-queue flood guard).
    * FAIL CLOSED: if a store call fails (including the cap counts), nothing is queued and the message is
      recorded ``queue-error``.
    * Tags ``decision-msg`` + ``from:<host>`` make it findable; with no project tag it is unrouted, so no brief
      serves it until a reviewer passes ``tags=["project:<p>"]`` to memory_approve.
    """
    seen = msg_store.get_status(m.id)
    if seen is not None and seen != "queue-error":
        logger.info("Decision %s already handled (%s); re-delivery skipped", m.id[:8], seen)
        return
    sender = _clean(m.from_agent, 64) or "unknown"
    proposer = f"msg:{sender}"
    name = normalise_name(f"decision-msg-{m.id}")
    try:
        per_sender = int(await _maybe_await(memory_store.pending_count_by_proposer(proposer)))
        total = int(await _maybe_await(memory_store.pending_count_by_proposer("msg:", prefix=True)))
        if per_sender >= MAX_PENDING_PER_SENDER or total >= MAX_PENDING_DECISIONS_TOTAL:
            _record(msg_store, m, "rejected-queue-full")
            logger.warning(
                "Decision %s from %s refused: queue full (%d from this sender, %d in all; caps %d/%d)",
                m.id[:8],
                sender,
                per_sender,
                total,
                MAX_PENDING_PER_SENDER,
                MAX_PENDING_DECISIONS_TOTAL,
            )
            return
        if await _maybe_await(memory_store.get_scope_rows([name])):
            _record(msg_store, m, "rejected-name-exists")
            logger.warning(
                "Decision %s from %s refused: a memory named %s already exists",
                m.id[:8],
                sender,
                name,
            )
            return
        provenance = {
            "actor": "msg",
            "actor_detail": sender,
            "source": "msg_daemon",
            "msg_id": m.id,
            "to": m.to,
            "ts": str(m.ts),
            "sender_verified": False,
        }
        await _maybe_await(
            memory_store.queue_pending_write(
                name=name,
                title=m.body[:80],
                body=m.body,
                type="project",
                tier="working",
                tags=["decision-msg", f"from:{sender}"],
                origin_clients=[sender],
                proposed_by=proposer,
                source="msg_daemon",
                provenance=json.dumps(provenance, sort_keys=True),
            )
        )
    except Exception:
        logger.exception(
            "Decision %s from %s NOT queued (store error); recorded queue-error", m.id[:8], sender
        )
        _record(msg_store, m, "queue-error")
        return
    _record(msg_store, m, "queued-for-review")
    logger.info("Decision %s from %s queued for review as %s", m.id[:8], sender, name)


async def _dispatch(
    msg_data: dict,
    raw_subject: str,
    msg_store: MsgStore,
    memory_store: BaseStore,
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
        await _queue_decision_for_review(m, msg_store, memory_store)

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


async def _run_once(msg_store: MsgStore, memory_store: BaseStore) -> None:
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
    # B1: the CONFIGURED backend (Postgres in production via MORI_DATABASE_URL), exactly as the server, so queued
    # decisions land in the review queue reviewers actually read, not a stray SQLite file.
    memory_store = get_store(DATA_DIR / "memories.db")
    await _maybe_await(memory_store.bootstrap())

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

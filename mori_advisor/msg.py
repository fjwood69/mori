"""mori-msg — inter-agent messaging over NATS JetStream.

Schema, publish helpers, and stream setup shared by the daemon and MCP tools.
"""

import json
import socket
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

import nats
from nats.js.errors import NotFoundError
from pydantic import BaseModel, ConfigDict, Field

MsgType = Literal["task", "decision", "question", "reply", "ack", "done", "broadcast"]

STREAM_NAME = "MORI_MSG"
STREAM_SUBJECTS = ["mori.msg.*", "mori.reply.*"]
STREAM_MAX_AGE = 7 * 86400  # 7 days, matches cc.share replay window


class MoriMessage(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    from_agent: str = Field(alias="from")
    to: str
    type: MsgType
    ts: str
    body: str
    reply_to: Optional[str] = None


def build_message(
    to: str,
    type: MsgType,
    body: str,
    reply_to: Optional[str] = None,
) -> MoriMessage:
    return MoriMessage.model_validate(
        {
            "id": str(uuid.uuid4()),
            "from": socket.gethostname(),
            "to": to,
            "type": type,
            "ts": datetime.now(timezone.utc).isoformat(),
            "body": body,
            "reply_to": reply_to,
        }
    )


def msg_subject(to: str) -> str:
    return "mori.msg.broadcast" if to == "broadcast" else f"mori.msg.{to}"


def reply_subject(original_id: str) -> str:
    return f"mori.reply.{original_id}"


async def _ensure_stream(js) -> None:
    try:
        await js.stream_info(STREAM_NAME)
    except NotFoundError:
        await js.add_stream(
            name=STREAM_NAME,
            subjects=STREAM_SUBJECTS,
            max_age=STREAM_MAX_AGE,
            storage="file",
            retention="limits",
        )


async def publish_message(nats_url: str, msg: MoriMessage) -> None:
    nc = await nats.connect(nats_url)
    js = nc.jetstream()
    await _ensure_stream(js)
    payload = json.dumps(msg.model_dump(by_alias=True)).encode()
    await js.publish(msg_subject(msg.to), payload)
    if msg.reply_to:
        await js.publish(reply_subject(msg.reply_to), payload)
    await nc.flush()
    await nc.drain()

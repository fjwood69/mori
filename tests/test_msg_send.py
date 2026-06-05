"""Regression tests for #37 — msg_send directed-task bug.

Before the fix:
  1. msg_send() published to NATS but never persisted to the local msg_log.
  2. get_thread() used exact UUID match; users only had the 8-char prefix from
     msg_send's return value, so msg_thread always returned "No message found".

After the fix:
  1. msg_send() persists the sent message with status="sent" to the local store.
  2. msg_send() returns the full UUID (not the 8-char prefix) so callers can
     pass it to msg_thread directly.
  3. get_thread() accepts both full UUID and 8-char prefix (fallback LIKE search).
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from mori_advisor.msg import MoriMessage

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_store(tmp_path: Path):
    from mori_advisor.store.sqlite_store import SQLiteStore

    s = SQLiteStore(tmp_path / "memories.db", msg_db_path=tmp_path / "msg.db")
    s.bootstrap()
    return s


def _apply_store(monkeypatch, store):
    import mori_advisor.main as m

    monkeypatch.setattr(m, "store", store)
    monkeypatch.setattr(m, "memory_store", store._mem if hasattr(store, "_mem") else store)


# ── #37 bug: msg_send persists to msg_log ────────────────────────────────────


def test_msg_send_persists_to_local_log(tmp_path, monkeypatch):
    """msg_send must write the sent message to msg_log (status='sent')."""
    store = _make_store(tmp_path)
    _apply_store(monkeypatch, store)

    from mori_advisor.main import msg_send

    with patch("mori_advisor.msg.publish_message", new_callable=AsyncMock):
        result = asyncio.run(msg_send(to="remote-host", type="task", body="hello"))

    assert "Sent [task] to remote-host" in result
    # The full UUID must appear in the return value (not just 8 chars)
    sent_id = result.split("id=")[-1].rstrip(")")
    assert len(sent_id) > 8, f"expected full UUID in result, got: {sent_id!r}"

    # Message must be in the local msg_log
    msgs = store.count_messages()
    assert msgs >= 1, "msg_log must contain the sent message"

    rows = store._msg.get_thread(sent_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "sent"
    assert rows[0]["to_host"] == "remote-host"
    assert rows[0]["body"] == "hello"
    assert rows[0]["type"] == "task"


def test_msg_send_returns_full_uuid(tmp_path, monkeypatch):
    """msg_send must return the full UUID so msg_thread can find it without guessing."""
    store = _make_store(tmp_path)
    _apply_store(monkeypatch, store)

    from mori_advisor.main import msg_send

    with patch("mori_advisor.msg.publish_message", new_callable=AsyncMock):
        result = asyncio.run(msg_send(to="other", type="question", body="q?"))

    sent_id = result.split("id=")[-1].rstrip(")")
    # A UUID4 is 36 chars; at minimum it must be longer than the old 8-char prefix
    assert len(sent_id) == 36, f"expected 36-char UUID, got {len(sent_id)!r} chars: {sent_id!r}"


# ── #37 bug: get_thread prefix fallback ──────────────────────────────────────


def test_get_thread_exact_uuid(tmp_path):
    """get_thread finds a message by full UUID."""
    store = _make_store(tmp_path)
    full_id = str(uuid.uuid4())
    from datetime import datetime, timezone

    msg = MoriMessage(
        id=full_id,
        from_agent="a",
        to="b",
        type="task",
        ts=datetime.now(timezone.utc).isoformat(),
        body="hi",
    )
    store._msg.upsert(msg, status="sent")

    rows = store._msg.get_thread(full_id)
    assert len(rows) == 1
    assert rows[0]["id"] == full_id


def test_get_thread_8char_prefix_fallback(tmp_path):
    """get_thread finds a message by 8-char prefix (backward compat with old msg_send output)."""
    store = _make_store(tmp_path)
    full_id = str(uuid.uuid4())
    from datetime import datetime, timezone

    msg = MoriMessage(
        id=full_id,
        from_agent="a",
        to="b",
        type="task",
        ts=datetime.now(timezone.utc).isoformat(),
        body="hi",
    )
    store._msg.upsert(msg, status="sent")

    prefix = full_id[:8]
    rows = store._msg.get_thread(prefix)
    assert len(rows) == 1
    assert rows[0]["id"] == full_id


def test_get_thread_not_found(tmp_path):
    """get_thread returns [] for an unknown id."""
    store = _make_store(tmp_path)
    assert store._msg.get_thread("nonexistent") == []
    assert store._msg.get_thread("12345678") == []


# ── full round-trip: send → thread ────────────────────────────────────────────


def test_msg_send_thread_roundtrip(tmp_path, monkeypatch):
    """End-to-end: send a task, retrieve the thread by the returned id."""
    store = _make_store(tmp_path)
    _apply_store(monkeypatch, store)

    from mori_advisor.main import msg_send, msg_thread

    with patch("mori_advisor.msg.publish_message", new_callable=AsyncMock):
        send_result = asyncio.run(msg_send(to="cb14p", type="task", body="do the thing"))

    sent_id = send_result.split("id=")[-1].rstrip(")")

    thread_result = asyncio.run(msg_thread(sent_id))
    assert "No message found" not in thread_result
    assert "do the thing" in thread_result
    assert "task" in thread_result
    assert "cb14p" in thread_result

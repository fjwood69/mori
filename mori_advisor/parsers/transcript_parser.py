"""CC Transcript parser — parses Claude Code .jsonl transcript files.

Reads session transcripts, groups by session_id, formats summaries similar
to DreamPipeline._format_events(), and produces chunks for distillation.

`--since` filtering uses the first event's `timestamp` field from inside
each JSONL file, with file mtime as a fast pre-filter for directories.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mori_advisor.parsers import BaseParser, Chunk, register_parser

logger = logging.getLogger(__name__)

# ~50K tokens ≈ 200K characters
CHUNK_CHAR_LIMIT = 200_000


def _parse_since_duration(since: str) -> timedelta | None:
    """Parse a duration string like '30d', '90d', '7d' into a timedelta.

    Returns None if the format is unrecognised.
    """
    if not since:
        return None
    since = since.strip().lower()
    if since.endswith("d"):
        try:
            return timedelta(days=int(since[:-1]))
        except ValueError:
            return None
    if since.endswith("w"):
        try:
            return timedelta(weeks=int(since[:-1]))
        except ValueError:
            return None
    # Try ISO date
    try:
        dt = datetime.fromisoformat(since)
        return datetime.now(timezone.utc) - dt
    except ValueError:
        return None


def _parse_first_timestamp(file_path: Path) -> datetime | None:
    """Extract the first event timestamp from a JSONL transcript file.

    Reads only the first few lines, looking for a valid JSON line with
    a 'timestamp' field. Returns a timezone-aware datetime or None.
    """
    try:
        with open(file_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    ts = data.get("timestamp")
                    if ts:
                        # Handles both ISO with timezone and naive (assume UTC)
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        return dt
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
    except Exception:
        pass
    return None


@register_parser("transcripts")
class TranscriptParser(BaseParser):
    """Parse CC JSONL transcripts into session-grouped chunks.

    Each session becomes one chunk (unless the session is very large,
    in which case it's split by event count).

    `--since`: filters by the first event timestamp inside each file.
    For directories, file mtime is used as a fast pre-filter before
    opening the file to check the actual timestamp.
    """

    @classmethod
    def can_handle(cls, source: Path) -> bool:
        if not source.exists():
            return False
        if source.is_dir():
            # Check if directory contains .jsonl files
            return any(f.suffix == ".jsonl" for f in source.iterdir() if f.is_file())
        return source.suffix.lower() == ".jsonl"

    def parse(self, source: Path, **kwargs) -> list[Chunk]:
        since_str = kwargs.get("since", "")
        cutoff = _parse_since_duration(since_str)
        if cutoff:
            cutoff_dt = datetime.now(timezone.utc) - cutoff
        else:
            cutoff_dt = None

        # Collect JSONL files
        if source.is_dir():
            jsonl_files = sorted(
                [f for f in source.rglob("*.jsonl") if f.is_file()]
            )
        else:
            jsonl_files = [source]

        chunks: list[Chunk] = []
        for jsonl_path in jsonl_files:
            # Fast pre-filter by mtime for directories
            if cutoff_dt and source.is_dir():
                mtime = datetime.fromtimestamp(
                    os.path.getmtime(jsonl_path), tz=timezone.utc
                )
                if mtime < cutoff_dt:
                    continue

            # Accurate filter by first event timestamp
            if cutoff_dt:
                first_ts = _parse_first_timestamp(jsonl_path)
                if first_ts and first_ts < cutoff_dt:
                    continue

            try:
                session_chunks = self._parse_file(jsonl_path)
                chunks.extend(session_chunks)
            except Exception as e:
                logger.warning("Failed to parse transcript %s: %s", jsonl_path, e)

        return chunks

    def _parse_file(self, file_path: Path) -> list[Chunk]:
        """Parse a single JSONL transcript file.

        Reads all events, groups by session_id (though most files contain
        a single session), and formats summary blocks.
        """
        sessions: dict[str, list[dict]] = {}
        try:
            with open(file_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                        sid = event.get("sessionId") or event.get("session_id") or "unknown"
                        sessions.setdefault(sid, []).append(event)
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            logger.warning("Error reading %s: %s", file_path, e)
            return []

        chunks: list[Chunk] = []
        for sid, events in sessions.items():
            formatted = self._format_session(sid, events)
            if not formatted.strip():
                continue

            # If formatted output is too large, split by event count
            if len(formatted) > CHUNK_CHAR_LIMIT:
                mid = len(events) // 2
                for i, batch in enumerate([events[:mid], events[mid:]]):
                    batch_text = self._format_session(sid, batch)
                    chunks.append(
                        Chunk(
                            content=batch_text,
                            metadata={
                                "source_path": str(file_path),
                                "session_id": sid,
                                "part": i + 1,
                                "event_count": len(batch),
                            },
                        )
                    )
            else:
                chunks.append(
                    Chunk(
                        content=formatted,
                        metadata={
                            "source_path": str(file_path),
                            "session_id": sid,
                            "event_count": len(events),
                        },
                    )
                )

        return chunks

    def _format_session(self, session_id: str, events: list[dict]) -> str:
        """Format a session's events into a signal-focused summary block.

        Similar to DreamPipeline._format_events() — extracts prompts,
        tool calls, errors, and stop reasons.
        """
        if not events:
            return ""

        first_ts = events[0].get("timestamp", "?")[:19]
        last_ts = events[-1].get("timestamp", "?")[:19]

        # Extract client/CWD from first event
        client = ""
        cwd = ""
        for e in events:
            if e.get("event_name") == "SessionStart" or e.get("cwd"):
                cwd = e.get("cwd", "")
                client = e.get("client", "")
                break

        parts = [f"Session: {session_id} ({first_ts} to {last_ts})"]
        if client:
            parts.append(f"Client: {client}")
        if cwd:
            parts.append(f"CWD: {cwd}")

        # Extract signal — prompts, tools, errors
        items = []
        for e in events:
            role = e.get("role", "")
            content = e.get("content", "")
            event_name = e.get("event_name", "")
            tool = e.get("tool_name", "")
            prompt = e.get("prompt", "")
            err = e.get("tool_error", "")

            # User prompts (from event log)
            if event_name == "UserPromptSubmit" and prompt:
                p_text = prompt[:300].replace("\n", " ")
                items.append(f"  User: {p_text}")

            # Assistant messages with text content
            elif role == "assistant" and content:
                # Skip tool_use blocks — handled separately
                pass

            # Tool calls
            elif event_name == "PostToolUse" and tool:
                items.append(f"  Tool: {tool}")

            # Tool failures
            elif event_name == "PostToolUseFailure" and err:
                items.append(f"  FAILURE ({tool}): {err[:150]}")

            # Stop reasons
            elif event_name == "Stop":
                reason = e.get("stop_reason", "end_turn")
                items.append(f"  Stopped: {reason}")

        if items:
            parts.append("Events:")
            parts.extend(items)

        return "\n".join(parts)
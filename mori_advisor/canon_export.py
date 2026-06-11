"""Structured canon export for external-LLM review.

Pure formatting service — NO HTTP / CLI / MCP / store imports. The REST route and the
MCP tool are thin adapters that fetch rows (via the store) and call :func:`build`.

Three formats:
  - ``standard``  — grouped, human-readable Markdown with a reproducibility pin header.
  - ``consult``   — the standard body prefixed by a COHERENCE rubric (internal-consistency
                    only; never asks a reviewer to judge reality it cannot observe).
  - ``json``      — ``{"meta": ..., "memories": [...]}``.

SAFETY: provenance / internal fields (session ids, client hostnames, …) are stripped by
default — an external review must not receive them. Only the allowlist below egresses
unless ``include_provenance=True`` is explicitly passed.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

# ── Field policy ──────────────────────────────────────────────────────────────
# Allowlist (safe to send to an external model). Everything else is stripped by default.
SAFE_FIELDS: tuple[str, ...] = (
    "name",
    "title",
    "description",
    "type",
    "tier",
    "tags",
    "body",
    "confidence",
    "retrieval_count",
    "last_retrieved_at",
    "created_at",
    "updated_at",
)
# Internal provenance / PII — never egress unless include_provenance=True.
PROVENANCE_FIELDS: tuple[str, ...] = (
    "origin_session_id",
    "origin_session_ids",
    "origin_clients",
    "protected_domains",
    "superseded_by",
    "freshness_checked_at",
)

# ── Grouping (hybrid: type → ranked tag → Uncategorized) ──────────────────────
# (label, set-of-types). A row lands in the first matching bucket by type; rows whose
# type matches none fall through to tag-based, then "Uncategorized".
_TYPE_GROUPS: list[tuple[str, set[str]]] = [
    ("Architecture & Decisions", {"decision"}),
    ("Conventions & Patterns", {"pattern", "convention"}),
    ("Standards & Practices", {"standard"}),
    ("Incidents & Lessons", {"incident"}),
]
# tags that, when type is unhelpful (project/empty), promote a row into a named bucket
_TAG_GROUPS: list[tuple[str, set[str]]] = [
    ("Architecture & Decisions", {"architecture", "decisions"}),
    ("Conventions & Patterns", {"conventions", "patterns"}),
    ("Standards & Practices", {"standards"}),
    ("Incidents & Lessons", {"incidents", "gotchas"}),
]
_UNCATEGORISED = "Uncategorized"

# ── The coherence rubric (no truth-verbs — see test_canon_export.banned-word test) ────
# Deliberately contains NONE of: true/valid/correct/accurate/right/wrong/false/stale/
# outdated/up-to-date — those ask the reviewer to judge reality it cannot observe.
COHERENCE_RUBRIC = """# Canon coherence review

You are reviewing a software project's canonical memory set for INTERNAL COHERENCE only.
You cannot see the codebase, so do not judge any memory against external reality — restrict
your review entirely to what the text itself says.

For each memory, flag it only if it is:
- CONTRADICTS — its claim conflicts with another memory's claim (name both)
- REDUNDANT — it substantially overlaps another memory (say which to merge)
- UNCLEAR — it uses undefined terms or an agent could not tell what to do from it
- CIRCULAR — it defines a thing in terms of itself, or asserts nothing an agent could act on
- THIN — it carries too little detail to guide an action

Output a list of SUGGESTIONS for the human Trusted-Dreamer to action — one of
merge / archive / clarify / split per flagged memory. These are suggestions for a human
to decide on, not decisions. Memories you do not flag need no comment.
"""


def sanitise(row: dict[str, Any], include_provenance: bool = False) -> dict[str, Any]:
    """Project a raw memory row onto the safe allowlist (drops provenance/PII by default)."""
    fields = SAFE_FIELDS + (PROVENANCE_FIELDS if include_provenance else ())
    out = {k: row.get(k) for k in fields if k in row}
    # normalise tags to a list (JSONB may arrive as list already; sqlite as JSON string)
    tags = out.get("tags")
    if isinstance(tags, str):
        try:
            out["tags"] = json.loads(tags)
        except (ValueError, TypeError):
            out["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    elif tags is None:
        out["tags"] = []
    return out


def _project_matches(row: dict[str, Any], project: str) -> bool:
    if not project:
        return True
    tags = row.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    low = {str(t).lower() for t in tags}
    p = project.lower()
    return (
        p in low or f"project:{p}" in low or "scope:global" in low or "scope:cross-project" in low
    )


def group_rows(
    rows: list[dict[str, Any]],
    strategy: str = "hybrid",
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Hybrid grouper: type first, then a ranked tag, then Uncategorized.

    Within each group, rows are sorted by retrieval_count descending (most-used first).
    Returns groups in a stable, presentation-friendly order.
    """
    buckets: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = [lbl for lbl, _ in _TYPE_GROUPS] + [_UNCATEGORISED]

    for row in rows:
        rtype = (row.get("type") or "").lower()
        label = None
        for lbl, types in _TYPE_GROUPS:
            if rtype in types:
                label = lbl
                break
        if label is None and strategy == "hybrid":
            tags = {str(t).lower() for t in (row.get("tags") or [])}
            for lbl, tagset in _TAG_GROUPS:
                if tags & tagset:
                    label = lbl
                    break
        if label is None:
            label = _UNCATEGORISED
        buckets.setdefault(label, []).append(row)

    result: list[tuple[str, list[dict[str, Any]]]] = []
    for lbl in order:
        if lbl in buckets:
            rows_sorted = sorted(
                buckets[lbl], key=lambda r: r.get("retrieval_count") or 0, reverse=True
            )
            result.append((lbl, rows_sorted))
    return result


def _fmt_ts(v: Any) -> str:
    """Best-effort ISO timestamp string (handles datetime, str, None)."""
    if v is None:
        return ""
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def _memory_block(row: dict[str, Any]) -> str:
    meta_bits = []
    if row.get("type"):
        meta_bits.append(f"**Type:** {row['type']}")
    if row.get("tier"):
        meta_bits.append(f"**Tier:** {row['tier']}")
    if row.get("confidence") is not None:
        meta_bits.append(f"**Confidence:** {row['confidence']}")
    if row.get("retrieval_count") is not None:
        meta_bits.append(f"**Retrievals:** {row['retrieval_count']}")
    lines = [f"### {row.get('name', '(unnamed)')}"]
    if row.get("title"):
        lines.append(f"_{row['title']}_")
    if meta_bits:
        lines.append(" | ".join(meta_bits))
    lines.append("")
    lines.append((row.get("body") or row.get("description") or "").strip())
    tags = row.get("tags") or []
    if tags:
        lines.append("")
        lines.append(f"**Tags:** {', '.join(str(t) for t in tags)}")
    return "\n".join(lines)


def _header(meta: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Mori Canon Export",
            f"Generated: {meta.get('generated_at', '')}",
            f"Instance: {meta.get('instance', '')}",
            f"Version: {meta.get('version', '')}",
            f"Canon size: {meta.get('canon_size', 0)} memories",
            f"Export scope: {meta.get('scope', 'all')}",
        ]
    )


def _standard(groups: list[tuple[str, list[dict]]], meta: dict[str, Any]) -> str:
    parts = [_header(meta), ""]
    for label, rows in groups:
        parts.append("---")
        parts.append(f"## {label}")
        parts.append("")
        for row in rows:
            parts.append(_memory_block(row))
            parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _consult(groups: list[tuple[str, list[dict]]], meta: dict[str, Any]) -> str:
    return COHERENCE_RUBRIC + "\n---\n\n## Memories to review\n\n" + _standard(groups, meta)


def _json_out(rows: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    return json.dumps({"meta": meta, "memories": rows}, indent=2, default=_fmt_ts)


def build(
    rows: Iterable[dict[str, Any]],
    fmt: str = "standard",
    meta: dict[str, Any] | None = None,
    include_provenance: bool = False,
    project: str = "",
    group_strategy: str = "hybrid",
) -> str:
    """Render canon rows as ``standard`` / ``consult`` / ``json``.

    Rows are sanitised onto the safe allowlist (provenance stripped unless asked), then
    project-filtered and grouped. ``meta`` carries the reproducibility pin (instance,
    version, generated_at). Unknown ``fmt`` falls back to ``standard``.
    """
    meta = dict(meta or {})
    safe = [sanitise(r, include_provenance) for r in rows if _project_matches(r, project)]
    meta.setdefault("canon_size", len(safe))
    if fmt == "json":
        return _json_out(safe, meta)
    groups = group_rows(safe, group_strategy)
    if fmt == "consult":
        return _consult(groups, meta)
    return _standard(groups, meta)

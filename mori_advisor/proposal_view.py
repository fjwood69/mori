"""What a reviewer sees for a proposal (v2.3.16, R3) — computed ONCE, on the server.

``memory_pending_list``, ``GET /api/pending/json`` and ``/review`` all render these fields, so the
three surfaces cannot disagree. For every proposal: its operation and a diff —

* ``write``: the proposed content against the current memory (or "new memory");
* ``rollback``: the target version's content (which the proposal carries) against the current one;
* ``delete``: the full current body that would be removed.

Plus the supersede chain (older proposals this one replaced) and whether the proposer changed along
it — the tripwire for one key shadowing another's proposal (consult C3).

PURE: no I/O. The stores fetch the target row and the chain, then call :func:`review_fields`.
"""

from __future__ import annotations

import difflib
import json
from typing import Any

from mori_advisor.canon_authority import CD_CANARY_TAG, is_cd_canary_name

FIELDS = ("title", "description", "type", "tier")
BODY_DIFF_MAX_LINES = 400


def _tags(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else []
        except ValueError:
            return [raw]
    return sorted(str(t) for t in raw) if isinstance(raw, (list, tuple)) else [str(raw)]


def body_diff(before: str, after: str) -> str:
    lines = list(
        difflib.unified_diff(
            (before or "").splitlines(),
            (after or "").splitlines(),
            fromfile="current",
            tofile="proposed",
            lineterm="",
        )
    )
    if len(lines) > BODY_DIFF_MAX_LINES:
        extra = len(lines) - BODY_DIFF_MAX_LINES
        lines = lines[:BODY_DIFF_MAX_LINES] + [f"… {extra} more diff lines"]
    return "\n".join(lines)


def review_fields(
    item: dict[str, Any], target: dict[str, Any] | None, chain: list[dict[str, Any]]
) -> dict[str, Any]:
    """The reviewer-facing fields for one proposal.

    ``item``: the proposal (``operation``, ``title``, ``description``, ``type``, ``tier``, ``body``,
    ``tags``, ``target_version_id``, ``proposed_by``). ``target``: the memory's ACTIVE row now, or
    None. ``chain``: the closed proposals this one replaced, oldest first.
    """
    operation = item.get("operation") or "write"
    current = target or {}
    diff: dict[str, Any]
    if operation == "delete":
        diff = {
            "kind": "delete",
            "summary": "deletes the memory" if target else "the memory no longer exists",
            "current_body": current.get("body") or "",
            "fields": [],
            "body_diff": "",
        }
    else:
        changes = []
        for f in FIELDS:
            before, after = current.get(f) or "", item.get(f) or ""
            if target is None or before != after:
                changes.append({"field": f, "before": before, "after": after})
        before_tags, after_tags = _tags(current.get("tags")), _tags(item.get("tags"))
        if target is None or before_tags != after_tags:
            changes.append({"field": "tags", "before": before_tags, "after": after_tags})
        if target is None:
            summary = "new memory"
        elif operation == "rollback":
            summary = f"rolls back to version {item.get('target_version_id')}"
        else:
            summary = "changes the memory"
        diff = {
            "kind": operation,
            "summary": summary,
            "current_body": "",
            "fields": changes,
            "body_diff": body_diff(current.get("body") or "", item.get("body") or "")
            if target is not None
            else "",
        }
    chain_view = [
        {
            "id": c.get("id"),
            "proposed_by": c.get("proposed_by"),
            "proposed_at": str(c.get("proposed_at") or ""),
            "operation": c.get("operation") or "write",
            "status": c.get("status"),
        }
        for c in chain
    ]
    proposers = {c.get("proposed_by") for c in chain} | {item.get("proposed_by")}
    # v2.3.17 (ruling, item 2): a proposal on a CD deploy canary gets the "DO NOT APPROVE" banner.
    # The TARGET's tags decide (a delete proposal copies them, but a write proposal may not), with
    # the proposal's own tags as the fallback when the row no longer exists.
    is_cd_canary = (
        is_cd_canary_name(item.get("name"))
        or CD_CANARY_TAG in _tags(current.get("tags"))
        or CD_CANARY_TAG in _tags(item.get("tags"))
    )
    return {
        "operation": operation,
        "diff": diff,
        "chain": chain_view,
        "proposer_changed": len(proposers) > 1,
        "is_cd_canary": is_cd_canary,
    }


def text_lines(item: dict[str, Any], fields: dict[str, Any], max_body_lines: int = 40) -> list[str]:
    """The diff as indented text, for ``memory_pending_list``."""
    d = fields["diff"]
    out = [f"     operation: {fields['operation']} — {d['summary']}"]
    if fields.get("is_cd_canary"):
        out.append("     ⛔ CD DEPLOY CANARY — DO NOT APPROVE (approving fails the next deploy)")
    if fields["proposer_changed"]:
        out.append("     ⚠ proposer changed along this proposal's chain")
    if fields["chain"]:
        out.append(f"     replaces: {', '.join('#' + str(c['id']) for c in fields['chain'])}")
    for ch in d["fields"]:
        out.append(f"     {ch['field']}: {ch['before']!r} → {ch['after']!r}")
    text = d["current_body"] if d["kind"] == "delete" else d["body_diff"]
    if text:
        body_lines = text.splitlines()
        shown = body_lines[:max_body_lines]
        out.extend("       " + ln for ln in shown)
        if len(body_lines) > max_body_lines:
            out.append(f"       … {len(body_lines) - max_body_lines} more lines (see /review)")
    return out

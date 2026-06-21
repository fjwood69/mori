"""Intake completeness check — the structural / anatomy rung.

Deterministic, zero-model. The first intake check that gates on memory *anatomy* (does the
record carry both a claim and a populated, corroborable warrant?) rather than memory *relations*
(supersedes/duplicate). Motivated by the compute-tax finding: an answer-without-warrant memory is
the expensive-incomplete class (it thrashes more than no memory and mostly fails).

Consult-blessed design (2026-06-11), kept strictly syntactic — contract enforcement, NOT semantic
quality judgement (that is the later Bosun/LLM rung):

  - Universal  : body present + a warrant (reason/evidence) present and non-trivial.
  - Conditional: if the dreamer self-tagged ``memory_type == "directive"`` (an actionable assertion —
                 "register at X", "pin below Y", "never call Z"), the warrant must carry at least one
                 syntactic anchor (file path / symbol / commit SHA / URL).

PURE FUNCTION: no side effects, no knowledge of TD queues or ``/brief``. The disposition router (which
withholds vs flags) lives elsewhere. Design consult-blessed 2026-06-11; logical home is a shared
validation module — kept in mori_advisor for now since it has zero mori deps (importing it from
mori_intake is circular-safe).
"""

import re

# Permissive anchor patterns (consult: prefer false-negatives — let vague-but-valid warrants pass to
# the next rung rather than over-flag and burn the TD's trust). Matches a backtick code ref, a
# file/path.ext, a snake_case or intercaps-CamelCase symbol, a commit SHA, or a URL.
_ANCHOR = re.compile(
    r"`[^`]+`"
    r"|[\w\-/]+\.[A-Za-z]{1,8}\b"
    r"|\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b"
    r"|\b[A-Za-z][a-z0-9]+[A-Z][A-Za-z0-9]*\b"
    r"|\b[0-9a-f]{7,40}\b"
    r"|https?://\S+"
)
_MIN_WARRANT = 10  # chars in the reason text, after trim

# Disposition (consult): pure schema violations auto-withhold from the TD queue (protocol enforcement,
# like rejecting malformed JSON) + feed back to the dreamer; a directive missing an anchor is a grey
# area → TD flag with a one-click override. 'ok' = passes. Never touches /brief (these are pre-canon).
_SEVERITY = {
    "empty-body": "withhold",
    "empty-warrant": "withhold",
    "unwarranted-directive": "flag",
    "ok": "ok",
}


def _warrant_parts(candidate):
    reason = (candidate.get("reason") or "").strip()
    ev = candidate.get("evidence") or []
    if isinstance(ev, str):
        ev = [ev]
    ev_text = " ".join(str(x) for x in ev).strip()
    return reason, ev_text


def validate_anatomy(candidate):
    """Validate one intake candidate's anatomy. Returns a dict:
        {"valid": bool, "layer": "anatomy", "reason": <code>, "severity": <disposition>}
    Codes: ok | empty-body | empty-warrant | unwarranted-directive.
    `candidate` is the dreamer's memory object (body, reason, evidence[, memory_type]).
    A missing/blank ``memory_type`` is treated as non-directive (only the universal rules apply), so
    the universal check ships safely before the dreamer prompt is updated to self-tag.
    """
    body = (candidate.get("body") or "").strip()
    if not body:
        code = "empty-body"
        return {"valid": False, "layer": "anatomy", "reason": code, "severity": _SEVERITY[code]}

    reason, ev_text = _warrant_parts(candidate)
    if len(reason) < _MIN_WARRANT and not ev_text:
        code = "empty-warrant"
        return {"valid": False, "layer": "anatomy", "reason": code, "severity": _SEVERITY[code]}

    if (candidate.get("memory_type") or "").strip().lower() == "directive":
        if not _ANCHOR.search(f"{reason} {ev_text}"):
            code = "unwarranted-directive"
            return {"valid": False, "layer": "anatomy", "reason": code, "severity": _SEVERITY[code]}

    return {"valid": True, "layer": "anatomy", "reason": "ok", "severity": "ok"}


def audit_completeness(body, description, *, seam, name="", log=None):
    """AUDIT-mode wrapper around :func:`validate_anatomy` for the ``store.write`` chokepoint.

    The completeness gate had no call site (built + tested 2026-06-12, never wired): the
    dreamer's own ``_write_memory`` and the direct MCP write both reach ``store.write`` with
    ``_skip_protection=True``, so anatomy was never enforced on the bulk of canon. This puts a
    single check at the one door every writer passes through — but in **audit mode only**: it
    emits a structured ``COMPLETENESS-AUDIT`` log line for non-conforming writes and **never
    blocks**. The block/withhold disposition is a later, deliberate flip once the audit window
    quantifies how much existing canon would fail (see RESUME-completeness-chokepoint).

    Field mapping mirrors the dreamer's call: ``body`` is the memory body; ``description`` is the
    warrant (the dreamer passes ``description=mem.get("reason")``). ``memory_type`` is intentionally
    NOT derived from the store's ``type`` taxonomy (project/decision/pattern/profile) — that is a
    different axis from the directive flag the conditional rule keys on — so only the universal
    rules (empty-body / empty-warrant) fire here. This degrades safely until the dreamer self-tags.

    Returns the :func:`validate_anatomy` verdict dict (callers in audit mode ignore it).
    """
    verdict = validate_anatomy({"body": body, "reason": description})
    if not verdict["valid"] and log is not None:
        log.warning(
            "COMPLETENESS-AUDIT seam=%s name=%s reason=%s severity=%s",
            seam,
            name or "?",
            verdict["reason"],
            verdict["severity"],
        )
    return verdict

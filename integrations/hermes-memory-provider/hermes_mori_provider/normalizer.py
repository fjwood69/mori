"""HermesEventNormalizer — translate hermes-agent memory writes into mori proposals.

Durability is signalled via YAML/JSON frontmatter in the memory content:

    ---
    memory_id: my-learning
    durability: durable
    ---
    The body text goes here.

Rules
-----
* If ``durability`` is "ephemeral" (case-insensitive) → drop (return None).
* If ``durability`` is absent AND the target basename matches any pattern in
  ``ephemeral_target_patterns`` (configurable) → drop (return None).
* Otherwise → DURABLE: build and return a proposal payload.

For DURABLE memories:
* ``name`` is ``hermes.<memory_id>`` when a ``memory_id`` is present in the
  frontmatter.  When absent, a stable slug is derived from the normalised body
  text (first 64 chars, slugified) — this is logged as a DEGRADED path because
  the name may drift if the content is later edited.
* Frontmatter is stripped from ``body`` before writing.
* ``idempotency_key`` = sha256(original content including frontmatter) so that
  the outbox idempotency guarantee survives restarts.
* All proposals carry the tag ``source:hermes`` plus any extra tags from
  frontmatter.

Retraction (``action == "remove"``)
-------------------------------------
mori never deletes canon — a retraction is modelled as a NEW proposal whose
body asserts the prior fact is retracted.  Name: ``hermes.<memory_id>.retracted``
when memory_id is known, else slug with ``.retracted`` suffix.  The ``type`` is
set to "decision" to signal intent.  The original body is embedded so a reviewer
can confirm what is being retracted.

All names are namespaced under ``hermes.`` so they cannot collide with
human-owned canon.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Targets that are implicitly ephemeral if no durability frontmatter is present.
_DEFAULT_EPHEMERAL_PATTERNS: list[str] = [
    r"(?i)scratch",
    r"(?i)temp",
    r"(?i)wip",
    r"(?i)draft",
    r"(?i)ephemeral",
]

# Maximum characters of content to use when deriving a fallback slug.
_SLUG_CONTENT_CHARS = 64


def _strip_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Split YAML-style ``---`` frontmatter from body text.

    Tries YAML ``---`` block first, then JSON ``{...}`` at the very start.
    Returns ``(meta, body)`` where *meta* may be empty and *body* is the
    remaining text with leading/trailing whitespace stripped.

    Does NOT require PyYAML — uses a simple key: value parser sufficient
    for the flat frontmatter fields this normaliser cares about.
    """
    content = content or ""

    # ── YAML-style frontmatter ─────────────────────────────────────────────
    yaml_match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)", content, re.DOTALL)
    if yaml_match:
        fm_raw, body = yaml_match.group(1), yaml_match.group(2)
        meta: dict[str, Any] = {}
        for line in fm_raw.splitlines():
            line = line.strip()
            if ":" in line:
                k, _, v = line.partition(":")
                k = k.strip()
                v = v.strip()
                # Coerce simple boolean strings
                if v.lower() == "true":
                    meta[k] = True
                elif v.lower() == "false":
                    meta[k] = False
                elif v.startswith("[") and v.endswith("]"):
                    # Minimal inline list: [a, b, c]
                    inner = v[1:-1]
                    meta[k] = [i.strip().strip("'\"") for i in inner.split(",") if i.strip()]
                else:
                    meta[k] = v.strip("'\"")
        return meta, body.strip()

    # ── JSON object at start ───────────────────────────────────────────────
    if content.lstrip().startswith("{"):
        brace_depth = 0
        end_idx = -1
        for i, ch in enumerate(content):
            if ch == "{":
                brace_depth += 1
            elif ch == "}":
                brace_depth -= 1
                if brace_depth == 0:
                    end_idx = i
                    break
        if end_idx != -1:
            try:
                meta = json.loads(content[: end_idx + 1])
                if isinstance(meta, dict):
                    return meta, content[end_idx + 1 :].strip()
            except json.JSONDecodeError:
                pass

    return {}, content.strip()


def _slugify(text: str) -> str:
    """Convert arbitrary text to a safe kebab-case slug for use in names.

    Collapses non-alphanumeric runs to a single hyphen and strips leading/
    trailing hyphens.  Truncated to 64 characters max.
    """
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text[:64]


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class HermesEventNormalizer:
    """Convert hermes-agent on_memory_write events into mori proposal payloads.

    Parameters
    ----------
    ephemeral_target_patterns:
        Regex patterns (compiled case-insensitively) applied against the target
        *basename* (last path component).  If any match AND no durability
        frontmatter is present, the event is dropped.  Pass an empty list to
        disable the regex fallback entirely.
    """

    def __init__(
        self,
        ephemeral_target_patterns: list[str] | None = None,
    ) -> None:
        patterns = (
            ephemeral_target_patterns
            if ephemeral_target_patterns is not None
            else _DEFAULT_EPHEMERAL_PATTERNS
        )
        self._ephemeral_re: list[re.Pattern[str]] = [re.compile(p) for p in patterns]

    def normalize(
        self,
        action: str,
        target: str,
        content: str,
    ) -> dict[str, Any] | None:
        """Normalise a hermes memory event into a mori proposal payload.

        Returns ``None`` if the event should be dropped (ephemeral).
        Returns a dict ready to pass to ``MoriRestClient.propose(**payload)``
        for durable events.

        The ``action`` parameter is expected to be one of:
          * "add"     — new memory
          * "replace" — update to existing memory
          * "remove"  — retraction request

        Unknown actions are treated as "add".
        """
        meta, body_text = _strip_frontmatter(content)

        # ── Durability decision ──────────────────────────────────────────────
        durability = str(meta.get("durability", "")).strip().lower()
        memory_id = str(meta.get("memory_id", "")).strip()
        extra_tags: list[str] = meta.get("tags", []) if isinstance(meta.get("tags"), list) else []
        mem_type: str = str(meta.get("type", "project")).strip() or "project"

        if durability == "ephemeral":
            logger.debug(
                "normalizer: dropping ephemeral event (frontmatter) action=%s target=%r",
                action,
                target,
            )
            return None

        if not durability:
            # No frontmatter signal — check target basename against regex patterns.
            basename = target.replace("\\", "/").split("/")[-1]
            for pat in self._ephemeral_re:
                if pat.search(basename):
                    logger.debug(
                        "normalizer: dropping ephemeral event (regex %r) action=%s target=%r",
                        pat.pattern,
                        action,
                        target,
                    )
                    return None

        # ── Name derivation ─────────────────────────────────────────────────
        if memory_id:
            base_name = f"hermes.{memory_id}"
        else:
            # Degraded path — derive from content.
            slug = _slugify(body_text[:_SLUG_CONTENT_CHARS]) or "unknown"
            base_name = f"hermes.{slug}"
            logger.warning(
                "normalizer: no memory_id in frontmatter for target=%r — "
                "using content-derived name %r (degraded path; name may drift)",
                target,
                base_name,
            )

        # Idempotency key is always from the ORIGINAL content (pre-strip) so
        # the outbox dedup survives restarts regardless of frontmatter presence.
        idempotency_key = _sha256(content)

        tags = ["source:hermes"] + extra_tags

        # ── Retraction branch ───────────────────────────────────────────────
        if action == "remove":
            retraction_name = f"{base_name}.retracted"
            retraction_body = (
                f"RETRACTION PROPOSAL\n\n"
                f"The hermes agent has requested removal of the memory "
                f"identified as `{base_name}`.\n\n"
                f"Original content at time of retraction request:\n\n"
                f"---\n{body_text or content}\n---\n\n"
                f"A human reviewer should confirm whether this memory should "
                f"be removed or downgraded in the canon."
            )
            return {
                "name": retraction_name,
                "title": f"Retraction: {base_name}",
                "description": f"Agent requested removal of {base_name}",
                "type": "decision",
                "body": retraction_body,
                "tags": tags + ["retraction"],
                "idempotency_key": idempotency_key,
            }

        # ── Normal durable proposal ─────────────────────────────────────────
        return {
            "name": base_name,
            "title": str(meta.get("title", base_name)) or base_name,
            "description": str(meta.get("description", "")) or "",
            "type": mem_type,
            "body": body_text,
            "tags": tags,
            "idempotency_key": idempotency_key,
        }

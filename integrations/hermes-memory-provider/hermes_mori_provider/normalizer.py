"""HermesEventNormalizer — translate hermes-agent memory writes into mori ops.

This module owns three concerns and NOTHING else:

1. **Name sanitisation** — mori memory names must match
   ``^[a-zA-Z0-9_-]{1,128}$`` (no dots, no slashes, no whitespace). Every name
   that leaves this module is guaranteed valid.
2. **Stable name derivation** — given a hermes ``on_memory_write`` event
   (``target`` in {"memory", "user"} plus ``content`` and optional
   ``metadata``) produce a deterministic ``hermes-{target}-{stable_key}`` name.
   The same logical memory always derives the same name so that
   ``replace``/``remove`` keep lineage with the prior ``add``.
3. **Action -> op mapping** — translate the real hermes action vocabulary
   ({"add", "replace", "remove"}) into the internal op the provider/outbox act
   on ({"propose", "supersede", "retract"}).

There is **no durability/ephemeral concept** and **no frontmatter parsing** --
those were invented against a fictional contract and have been removed. The
real ``on_memory_write`` only fires for the agent's built-in memory tool
editing MEMORY.md / USER.md, so every event is canon-worthy and is mirrored.

Name derivation rules
---------------------
``stable_key`` is derived as follows:

* ``target == "user"``  -> ``metadata["user_id"]`` (default ``"default"``).
* ``target == "memory"`` ->
    * ``metadata["memory_id"]`` when present, else
    * a deterministic slug of the first ~64 chars of ``content`` plus a short
      content-hash suffix (``-<8 hex>``) so two different memories that share a
      slug prefix do not collide. NEVER a random UUID.

The full name is then ``hermes-{target}-{stable_key}`` run through
``sanitize_name`` which strips invalid characters, collapses consecutive
hyphens, and right-truncates the *stable_key portion* to keep the total <= 128
while always preserving the ``hermes-{target}-`` prefix.

Intake stable_key mapping
-------------------------
The intake service requires an *eligibility-namespaced* ``stable_key`` that
satisfies the server-side namespace gate.  The mapping is:

* ``target == "memory"`` -> ``learned-{suffix}`` where ``{suffix}`` is the
  same suffix used for the mori name derivation (``memory_id`` when present,
  else ``slug-hash8``).  Prefix ``learned-`` is always used regardless of the
  original suffix contents.
* ``target == "user"``   -> ``preference-{user_id}`` where ``user_id`` comes
  from ``metadata["user_id"]`` (default ``"default"``).

The mori NAME (``hermes-{target}-{stable_key}``) used by LWM and reconcile is
unchanged.  Only the intake submission uses the eligibility-namespaced key.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from typing import Any

logger = logging.getLogger(__name__)

# mori name constraint.
_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
_MAX_NAME_LEN = 128

# Characters of content used when deriving a fallback slug.
_SLUG_CONTENT_CHARS = 64

# Real hermes targets.
_VALID_TARGETS = ("memory", "user")

# action -> internal op.
_ACTION_OP = {
    "add": "propose",
    "replace": "supersede",
    "remove": "retract",
}


def content_hash(content: str) -> str:
    """Return the SHA-256 hex digest of *content* using the canonical form.

    Shared contract with ``mori_intake.normalize.content_hash`` (INTAKE-05):
    applies Unicode NFKC normalisation then collapses all internal whitespace
    to a single space before hashing.  This ensures provider and intake
    produce identical digests for the same logical content — including text
    with composed vs decomposed Unicode characters or inconsistent whitespace.

    Previously this function hashed the raw content without normalisation,
    causing digest divergence whenever the intake service normalised the body
    before computing its hash.
    """
    canonical = " ".join(unicodedata.normalize("NFKC", content or "").split())
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def action_to_op(action: str) -> str:
    """Map a hermes action to the internal op.

    Unknown actions fall back to ``"propose"`` (safest: emit a proposal rather
    than silently drop a write).
    """
    return _ACTION_OP.get((action or "").strip().lower(), "propose")


def _slugify(text: str) -> str:
    """Convert arbitrary text to a safe kebab-case slug fragment.

    Lower-cases, collapses non-alphanumeric runs to a single hyphen, strips
    leading/trailing hyphens. Truncated to ``_SLUG_CONTENT_CHARS`` characters.
    Does NOT guarantee the mori name constraint on its own -- callers must still
    run the assembled name through :func:`sanitize_name`.
    """
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text[:_SLUG_CONTENT_CHARS]


def is_valid_name(name: str) -> bool:
    """Return True iff *name* satisfies the mori name constraint."""
    return bool(_NAME_RE.match(name or ""))


def sanitize_name(raw: str, *, prefix: str = "") -> str:
    """Coerce *raw* into a valid mori name (``^[a-zA-Z0-9_-]{1,128}$``).

    * Strips every character outside ``[a-zA-Z0-9_-]`` (replacing runs with a
      single hyphen).
    * Collapses consecutive hyphens into one.
    * Strips leading/trailing hyphens.
    * Right-truncates to ``_MAX_NAME_LEN`` while preserving *prefix* -- that is,
      truncation only eats into the suffix that follows *prefix* so the
      ``hermes-{target}-`` namespace is never lost. (If *prefix* itself exceeds
      the limit it is hard-truncated, but that cannot happen for the fixed
      ``hermes-memory-``/``hermes-user-`` prefixes this module uses.)
    * Guarantees a non-empty result (``"unknown"`` fallback) so the name is
      never the empty string.
    """
    raw = raw or ""

    def _clean(s: str) -> str:
        s = re.sub(r"[^a-zA-Z0-9_-]+", "-", s)
        s = re.sub(r"-{2,}", "-", s)
        return s.strip("-")

    # Clean the prefix but KEEP a single trailing hyphen as the separator
    # between the namespace prefix and the stable-key suffix.
    prefix_clean = _clean(prefix) if prefix else ""
    sep_prefix = f"{prefix_clean}-" if prefix_clean else ""

    if sep_prefix and raw.startswith(prefix):
        suffix_clean = _clean(raw[len(prefix) :])
        budget = _MAX_NAME_LEN - len(sep_prefix)
        if budget <= 0:
            # Degenerate: prefix alone fills the budget. Hard-truncate.
            result = sep_prefix[:_MAX_NAME_LEN].rstrip("-")
        else:
            suffix_clean = suffix_clean[:budget].rstrip("-")
            result = f"{sep_prefix}{suffix_clean}" if suffix_clean else prefix_clean
    else:
        result = _clean(raw)[:_MAX_NAME_LEN].rstrip("-")

    if not result:
        result = "unknown"
    return result


class HermesEventNormalizer:
    """Translate hermes ``on_memory_write`` events into mori name + op data.

    Stateless: holds no configuration. Constructed once and reused.
    """

    def derive_name(
        self,
        target: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Return the deterministic, sanitised mori name for an event.

        ``hermes-{target}-{stable_key}`` where *stable_key* is metadata-driven
        for ``user`` and metadata-or-content-driven for ``memory``.
        """
        metadata = metadata or {}
        target_norm = self._norm_target(target)

        if target_norm == "user":
            stable_key = str(metadata.get("user_id", "default")).strip() or "default"
        else:  # memory
            memory_id = str(metadata.get("memory_id", "")).strip()
            if memory_id:
                stable_key = memory_id
            else:
                slug = _slugify(content[:_SLUG_CONTENT_CHARS]) or "memory"
                suffix = content_hash(content)[:8]
                stable_key = f"{slug}-{suffix}"

        prefix = f"hermes-{target_norm}-"
        return sanitize_name(f"{prefix}{stable_key}", prefix=prefix)

    def derive_intake_stable_key(
        self,
        target: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Return the eligibility-namespaced stable_key for the intake service.

        This key must satisfy the intake server's namespace gate.  It is ONLY
        used for the intake submission payload; the mori name (used by LWM and
        reconcile) is derived separately by :meth:`derive_name`.

        Mapping:
          * ``target == "memory"`` -> ``learned-{suffix}``
            where ``{suffix}`` is the same raw suffix used in the mori name
            (``memory_id`` or ``slug-hash8``), so the key is stable across
            add/replace/remove calls for the same logical memory.
          * ``target == "user"``   -> ``preference-{user_id}``
            where ``user_id`` comes from ``metadata["user_id"]``
            (default ``"default"``).

        The returned key is plain text — no sanitisation for mori-name constraints
        is needed here (the intake server stores it as a plain text field).
        """
        metadata = metadata or {}
        target_norm = self._norm_target(target)

        if target_norm == "user":
            user_id = str(metadata.get("user_id", "default")).strip() or "default"
            return f"preference-{user_id}"
        else:  # memory
            memory_id = str(metadata.get("memory_id", "")).strip()
            if memory_id:
                suffix = memory_id
            else:
                slug = _slugify(content[:_SLUG_CONTENT_CHARS]) or "memory"
                suffix = f"{slug}-{content_hash(content)[:8]}"
            return f"learned-{suffix}"

    def normalize(
        self,
        action: str,
        target: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Translate an event into a normalised descriptor.

        Returns a dict with:
          * ``op``                — "propose" | "supersede" | "retract"
          * ``action``            — original hermes action ("add" | "replace" | "remove")
          * ``name``              — sanitised mori name (hermes-{target}-{key})
          * ``intake_stable_key`` — eligibility-namespaced key for the intake service
          * ``target``            — normalised target ("memory" | "user")
          * ``content``           — the raw content (body)
          * ``content_hash``      — sha256 of content
          * ``title`` / ``description`` / ``type`` / ``tags`` — proposal fields

        Never returns ``None`` -- every real ``on_memory_write`` is
        canon-worthy.
        """
        metadata = metadata or {}
        action_norm = (action or "").strip().lower()
        op = action_to_op(action_norm)
        target_norm = self._norm_target(target)
        name = self.derive_name(target, content, metadata)
        intake_key = self.derive_intake_stable_key(target, content, metadata)

        chash = content_hash(content)
        mem_type = str(metadata.get("type", "")).strip() or (
            "user" if target_norm == "user" else "project"
        )
        extra_tags = metadata.get("tags", [])
        if not isinstance(extra_tags, list):
            extra_tags = []
        tags = ["source:hermes", f"target:{target_norm}", *extra_tags]

        title = str(metadata.get("title", "")).strip() or name
        description = str(metadata.get("description", "")).strip()

        return {
            "op": op,
            "action": action_norm,
            "name": name,
            "intake_stable_key": intake_key,
            "target": target_norm,
            "content": content,
            "content_hash": chash,
            "title": title,
            "description": description,
            "type": mem_type,
            "tags": tags,
        }

    @staticmethod
    def _norm_target(target: str) -> str:
        target_norm = (target or "").strip().lower()
        if target_norm not in _VALID_TARGETS:
            logger.warning("normalizer: unexpected target %r — namespacing as 'memory'", target)
            return "memory"
        return target_norm

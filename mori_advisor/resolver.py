"""Legacy-tag → scope-map resolver — the H2 subsumption bridge.

The flat primitive in ``scope.py`` decides keep/drop from a memory's ``ScopeMap``
and a context-tag set. This module supplies the two compilations that let that
generic filter **exactly reproduce** the special-cased routing in
``get_memories_by_project`` (memory_store.py / postgres_store.py), so the cutover
is provably subtractive (Phase 5 parity gate, published 0/20 commitment).

Two halves:

  * ``compile_memory_scope(row)`` — a row's *effective* ScopeMap. An explicit
    ``scope`` column (migration 15) wins; otherwise the scope is **derived** from
    the row's legacy ``tags`` + ``type`` so an un-migrated corpus routes
    identically (NULL scope ⇒ no behaviour change).

  * ``compile_context_tags(project, strict_global)`` — the brief request's
    context-tag set. The whole ``MORI_BRIEF_SCOPE`` safe/all flag collapses to the
    **presence of a single context tag**, ``legacy:type-global``: present in "all"
    mode, absent in "safe" mode. That is the entire subsumption — the auto-global
    negation at memory_store.py:1538 becomes positive tag membership.

Mapping back to the legacy oracle (the only non-membership rules it has):

  | legacy rule (memory_store.py)                     | compiled to                       |
  |---------------------------------------------------|-----------------------------------|
  | ``project:P`` in tags  (L1519, project lane)      | scope tag ``project:P``           |
  | ``scope:global`` / ``scope:cross-project`` (L1543)| empty scope ⇒ global (always in)  |
  | ``type IN (profile,pattern)`` auto-global (L1538) | scope tag ``legacy:type-global``  |
  | ``strict_global`` safe/all flag (main.py:790)     | ``legacy:type-global`` in context |

Truth table (one project ``P``, context = safe ``{project:P}`` / all
``{project:P, legacy:type-global}``):

  * ``project:P`` row            → scope {project:P}            → in (safe & all)
  * explicit ``scope:global`` row→ scope {} (global)           → in (safe & all)
  * profile/pattern, no project  → scope {legacy:type-global}  → out(safe) in(all)
  * profile/pattern + project:P  → scope {project:P, legacy:…} → in (safe & all)
  * other-project ``project:Q``  → scope {project:Q}           → out (safe & all)

Pure + dependency-light (only ``scope.ScopeMap``); the lane partition and tier
asymmetry live in the store method that consumes these, not here.
"""

from __future__ import annotations

from mori_advisor.scope import ScopeMap

# Explicit cross-project tags — a row carrying either is global regardless of type
# (memory_store.py:1543 / postgres_store.py:968).
EXPLICIT_GLOBAL_TAGS = frozenset({"scope:global", "scope:cross-project"})

# Types that the LEGACY oracle auto-globalises when strict_global is False
# (memory_store.py:1538). H2 keeps the behaviour but makes it gate-able: compiled
# to this positive scope tag and matched only when the context opts in ("all").
LEGACY_AUTO_GLOBAL_TYPES = frozenset({"profile", "pattern"})
LEGACY_TYPE_GLOBAL_TAG = "legacy:type-global"

# Sentinel for a row the legacy oracle routes NOWHERE: no project: tag, no explicit
# global tag, not an auto-global type. An *empty* ScopeMap means GLOBAL (always in),
# so an unrouted row must NOT compile to empty — it carries a tag no context ever
# holds, making in_scope always False. This preserves the legacy fact that a bare,
# project-less, non-global memory is surfaced only via list()/search(), never brief.
UNROUTED_TAG = "legacy:unrouted"


def compile_memory_scope(row: dict) -> ScopeMap:
    """Derive a row's effective ScopeMap.

    An explicit ``scope`` column wins (the migrated, H2-native case). When it is
    absent/NULL the scope is derived from legacy ``tags`` + ``type`` so an
    un-migrated row routes byte-identically to the legacy oracle:

      * any ``scope:global`` / ``scope:cross-project`` tag ⇒ **global** (empty
        ScopeMap), short-circuiting all other rules — mirrors the legacy global
        lane, which surfaces such a row irrespective of its ``project:`` tags;
      * every ``project:<X>`` tag becomes a scope tag;
      * ``type`` in {profile, pattern} adds ``legacy:type-global``.

    ``match`` is always ``"any"`` for derived scopes (membership is a union of the
    row's homes — the legacy lanes are OR-combined). ``match="all"`` is reachable
    only via an explicit scope column, i.e. genuinely H2-native memories.
    """
    raw_scope = row.get("scope")
    if raw_scope not in (None, ""):
        # Migrated / H2-native: the stored map is authoritative.
        return ScopeMap.parse(raw_scope)

    tags = row.get("tags") or []
    if not isinstance(tags, (list, tuple, set, frozenset)):
        # Defensive: a stringified tag blob shouldn't reach here (the store dicts
        # parse tags to a list), but never raise on the hot path.
        tags = []
    tagset = {str(t) for t in tags}

    # Explicit global wins outright — global lane ignores project tags (L1549/974
    # exclude a project:P row from the *global* query, but such a row is still
    # surfaced via the project lane, so the union is unchanged either way).
    if tagset & EXPLICIT_GLOBAL_TAGS:
        return ScopeMap()  # global

    scope_tags = {t for t in tagset if t.startswith("project:")}
    if str(row.get("type", "")) in LEGACY_AUTO_GLOBAL_TYPES:
        scope_tags.add(LEGACY_TYPE_GLOBAL_TAG)

    # No routing tags ⇒ unrouted, NOT global. Sentinel keeps in_scope always False
    # (an empty ScopeMap would wrongly read as global — see UNROUTED_TAG).
    if not scope_tags:
        return ScopeMap(tags=frozenset({UNROUTED_TAG}), match="any")

    return ScopeMap(tags=frozenset(scope_tags), match="any")


def compile_context_tags(project: str | None, strict_global: bool) -> frozenset[str]:
    """Compile a brief request into the context-tag set the filter matches against.

    ``project`` contributes ``project:<P>`` (when non-empty). The ``strict_global``
    flag — "safe" (True) vs "all" (False) — is encoded purely as the presence of
    ``legacy:type-global``: absent in safe mode (auto-global profile/pattern rows
    drop out), present in "all" mode (they surface). No other state.

    With ``project`` empty and ``strict_global`` True (the unscoped safe brief,
    main.py:881) the context is empty ⇒ only genuinely global (empty-scope) rows
    match, which is exactly the unscoped global lane.
    """
    ctx: set[str] = set()
    if project:
        ctx.add(f"project:{project}")
    if not strict_global:
        ctx.add(LEGACY_TYPE_GLOBAL_TAG)
    return frozenset(ctx)

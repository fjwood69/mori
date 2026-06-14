"""Provenance Filter MVP — store-level strict_global scope routing.

The leak being fixed: `type IN (profile, pattern)` auto-globalizes a memory into EVERY
project's brief, so an origin-bound memory mistyped 'pattern' leaks cross-repo. In strict
mode a memory reaches the global/transferable lane ONLY via an explicit scope tag.
"""


def _store(tmp_path):
    from mori_advisor.store import get_store

    s = get_store(tmp_path / "m.db")
    s.bootstrap()
    return s._mem if hasattr(s, "_mem") else s


def _write(ms, name, **kw):
    d = dict(title=name, body="body", type="project", tier="working", tags=[])
    d.update(kw)
    return ms.write(name=name, **d)


def test_strict_global_drops_type_autoglobal(tmp_path):
    ms = _store(tmp_path)
    _write(ms, "proj-mem", tags=["project:foo"])
    _write(ms, "pattern-leak", type="pattern", tags=[])  # would auto-global by type in legacy
    _write(ms, "profile-leak", type="profile", tags=[])
    _write(ms, "explicit-global", tags=["scope:global"])

    # Legacy (strict_global=False): type=profile/pattern leak into the global lane.
    legacy = ms.get_memories_by_project("foo", include_global=True, strict_global=False)
    legacy_global = {m["name"] for m in legacy["global_memories"]}
    assert "pattern-leak" in legacy_global
    assert "profile-leak" in legacy_global
    assert "explicit-global" in legacy_global

    # Strict (strict_global=True): only the explicit scope tag globalizes.
    strict = ms.get_memories_by_project("foo", include_global=True, strict_global=True)
    strict_global = {m["name"] for m in strict["global_memories"]}
    assert "pattern-leak" not in strict_global, (
        "type=pattern must NOT auto-globalize in strict mode"
    )
    assert "profile-leak" not in strict_global, (
        "type=profile must NOT auto-globalize in strict mode"
    )
    assert "explicit-global" in strict_global, "explicit scope:global must still globalize"

    # Project-scoped body is unaffected by the strict flag.
    assert {m["name"] for m in strict["project_memories"]} == {"proj-mem"}


def test_strict_global_default_is_legacy(tmp_path):
    """Store default (strict_global omitted) preserves legacy behaviour — no surprise to
    other callers; only brief() in safe scope opts into strict."""
    ms = _store(tmp_path)
    _write(ms, "pattern-mem", type="pattern", tags=[])
    res = ms.get_memories_by_project("foo", include_global=True)  # default
    assert "pattern-mem" in {m["name"] for m in res["global_memories"]}

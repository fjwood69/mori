"""Tests for the structural completeness check (mori_advisor.completeness.validate_anatomy)."""

import logging

from mori_advisor.completeness import audit_completeness, validate_anatomy


def _c(**kw):
    base = {"body": "register the Mark inline parser", "reason": "", "evidence": []}
    base.update(kw)
    return base


# --- universal rules (apply to every candidate, regardless of memory_type) ---


def test_empty_body_withholds():
    r = validate_anatomy(_c(body="   ", reason="because the parser must run first"))
    assert not r["valid"] and r["reason"] == "empty-body" and r["severity"] == "withhold"


def test_empty_warrant_withholds():
    # body present but no reason and no evidence -> pure schema violation, auto-withhold
    r = validate_anatomy(_c(reason="", evidence=[]))
    assert not r["valid"] and r["reason"] == "empty-warrant" and r["severity"] == "withhold"


def test_trivial_reason_withholds():
    r = validate_anatomy(_c(reason="ok"))  # < min warrant length, no evidence
    assert not r["valid"] and r["reason"] == "empty-warrant"


def test_descriptive_with_prose_warrant_passes():
    # a descriptive memory needs only a non-trivial warrant — no anchor required
    r = validate_anatomy(
        _c(
            memory_type="descriptive",
            reason="the codebase consistently mirrors existing extensions",
        )
    )
    assert r["valid"] and r["reason"] == "ok"


# --- conditional rule (directive must carry a syntactic anchor) ---


def test_directive_without_anchor_flags():
    # the compute-tax failure case: an answer ("register at 750") with no codebase warrant
    r = validate_anatomy(
        _c(memory_type="directive", reason="register the parser at priority 750", evidence=[])
    )
    assert not r["valid"] and r["reason"] == "unwarranted-directive" and r["severity"] == "flag"


def test_directive_with_evidence_anchor_passes():
    r = validate_anatomy(
        _c(
            memory_type="directive",
            reason="register below the Equation parser",
            evidence=["extension/equation.go"],
        )
    )
    assert r["valid"] and r["reason"] == "ok"


def test_directive_with_symbol_anchor_in_reason_passes():
    r = validate_anatomy(
        _c(memory_type="directive", reason="register Mark below `Equation` which sits at 500")
    )
    assert r["valid"]


def test_directive_with_snake_case_anchor_passes():
    r = validate_anatomy(
        _c(memory_type="directive", reason="call parser.scan_delimiter before capping the run")
    )
    assert r["valid"]


# --- graceful default: missing memory_type behaves as non-directive (universal only) ---


def test_missing_memory_type_only_universal():
    # no memory_type tag yet (pre-prompt-update): a bare directive-shaped body still passes if it has
    # a non-trivial warrant — the directive anchor rule only activates once the dreamer self-tags.
    r = validate_anatomy(_c(reason="this keeps the extension below the core parsers"))
    assert r["valid"] and r["reason"] == "ok"


def test_governed_vs_unjustified_mirrors_the_finding():
    # governed (answer + codebase warrant) passes; unjustified (bare answer) is flagged — the 100% vs
    # 45% anatomy distinction the study measured, now enforced at intake.
    governed = validate_anatomy(
        _c(
            memory_type="directive",
            reason="register at 250 because the Equation parser owns `=` at 500",
        )
    )
    unjustified = validate_anatomy(_c(memory_type="directive", reason="register at 250"))
    assert governed["valid"] and not unjustified["valid"]


# --- audit_completeness: the AUDIT-mode wrapper used at the store.write chokepoint ---


def test_audit_logs_on_invalid_anatomy(caplog):
    log = logging.getLogger("mori_advisor.test")
    with caplog.at_level(logging.WARNING, logger="mori_advisor.test"):
        r = audit_completeness("a body", "", seam="store.write:test", name="bad-mem", log=log)
    assert not r["valid"] and r["reason"] == "empty-warrant"
    assert "COMPLETENESS-AUDIT" in caplog.text
    assert "seam=store.write:test" in caplog.text and "name=bad-mem" in caplog.text


def test_audit_silent_on_valid_anatomy(caplog):
    log = logging.getLogger("mori_advisor.test")
    with caplog.at_level(logging.WARNING, logger="mori_advisor.test"):
        r = audit_completeness(
            "a body", "a perfectly good warrant", seam="store.write:test", name="ok-mem", log=log
        )
    assert r["valid"]
    assert "COMPLETENESS-AUDIT" not in caplog.text


def test_audit_never_raises_without_logger():
    # no logger passed → still returns a verdict, never blocks/raises (audit is non-fatal)
    r = audit_completeness("", "", seam="store.write:test")
    assert not r["valid"] and r["reason"] == "empty-body"


# --- the chokepoint contract: MemoryStore.write invokes the audit, but NEVER blocks ---


def test_sqlite_write_invokes_audit_and_does_not_block(tmp_path, monkeypatch):
    from mori_advisor.memory_store import MemoryStore
    from mori_advisor.store.migrations import MIGRATIONS, apply_sqlite

    db = tmp_path / "memories.db"
    MemoryStore.bootstrap_schema(db)
    apply_sqlite(db, tuple(mig for mig in MIGRATIONS if mig.target == "memories"))
    st = MemoryStore(db)

    calls = []
    import mori_advisor.completeness as comp

    real = comp.validate_anatomy

    def _spy(candidate):
        calls.append(candidate)
        return real(candidate)

    monkeypatch.setattr(comp, "validate_anatomy", _spy)

    # a warrantless write (description empty) — would FAIL anatomy, but audit mode must not block it
    result = st.write(
        name="ungated-mem", title="t", body="a body with no warrant", type="project", tier="working"
    )
    assert "written" in result.lower()  # write succeeded — audit did not block
    assert calls, "store.write must invoke validate_anatomy (the chokepoint contract)"
    assert calls[0]["body"] == "a body with no warrant"
    # and the row is really there
    assert "a body with no warrant" in st.read("ungated-mem")

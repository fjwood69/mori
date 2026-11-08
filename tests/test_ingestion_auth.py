"""v2.3.16 (board ruling, 7 Nov): the ingestion server checks the main server's key table.

Before: it authenticated only against the legacy MORI_ADVISOR_API_KEY, which production does not set,
and an empty key let every request through — measured from the tailnet, /api/ingest/status answered
200 with no key and with a wrong key. Now status, job and upload need a key from MORI_API_KEYS with
the write role or above; health and ready stay open; the process refuses to start with no keys, and
in host mode with keys (the R5 rule). Uploads keep ingestion's provenance, so a dreamer key at this
door is not canon authority.
"""

from __future__ import annotations

import logging

import pytest
from starlette.testclient import TestClient

WRITE_KEY, WRITE_SECRET = "dev-console", "w" * 32
DREAM_KEY, DREAM_SECRET = "fred-review", "d" * 32
READ_KEY, READ_SECRET = "viewer", "r" * 32


@pytest.fixture
def keyed(monkeypatch):
    """The key table and roles, as init_auth/init_policy would load them (api mode)."""
    import mori_advisor.auth as auth
    import mori_advisor.policy as pol

    monkeypatch.setattr(
        auth, "_KEYS", {WRITE_KEY: WRITE_SECRET, DREAM_KEY: DREAM_SECRET, READ_KEY: READ_SECRET}
    )
    monkeypatch.setattr(pol, "_TD_MODE", "api")
    monkeypatch.setattr(pol, "_ROLES", {WRITE_KEY: "write", DREAM_KEY: "dreamer", READ_KEY: "read"})


@pytest.fixture
def client():
    import mori_advisor.ingestion_server as ing

    return TestClient(ing.app)  # no lifespan: the auth dependency runs before any handler


def _job(client, secret):
    headers = {"X-Api-Key": secret} if secret is not None else {}
    return client.get("/api/ingest/job/does-not-exist", headers=headers).status_code


def test_no_key_is_refused(keyed, client):
    assert _job(client, None) == 401


def test_an_empty_key_is_refused(keyed, client):
    assert _job(client, "") == 401


def test_a_wrong_key_is_refused(keyed, client):
    assert _job(client, "not-a-key") == 401


def test_a_read_key_is_refused(keyed, client):
    assert _job(client, READ_SECRET) == 403


@pytest.mark.parametrize("secret", [WRITE_SECRET, DREAM_SECRET])
def test_a_write_or_dreamer_key_is_accepted(keyed, client, secret):
    assert _job(client, secret) == 404  # auth passed; the job simply does not exist


def test_status_and_upload_are_guarded_too(keyed, client):
    assert client.get("/api/ingest/status").status_code == 401
    assert client.get("/api/ingest/status", headers={"X-Api-Key": READ_SECRET}).status_code == 403
    r = client.post("/api/ingest/upload", files={"files": ("a.md", b"x")})
    assert r.status_code == 401
    r = client.post(
        "/api/ingest/upload", files={"files": ("a.md", b"x")}, headers={"X-Api-Key": "nope"}
    )
    assert r.status_code == 401


def test_open_mode_does_not_open_the_door(monkeypatch, client):
    """No keys loaded (auth's 'anonymous' open mode) must NOT pass the ingestion check."""
    import mori_advisor.auth as auth

    monkeypatch.setattr(auth, "_KEYS", {})
    assert _job(client, None) == 401


def test_open_mode_with_a_key_sent_does_not_pass(monkeypatch, client):
    """With no keys loaded, auth.check_key returns 'anonymous' for ANY key sent. That must not pass
    the door — even if 'anonymous' were somehow given the write role (reserved names refuse that
    in MORI_API_KEY_ROLES; the door must not rely on it)."""
    import mori_advisor.auth as auth
    import mori_advisor.policy as pol

    monkeypatch.setattr(auth, "_KEYS", {})
    monkeypatch.setattr(pol, "_ROLES", {"anonymous": "write"})
    assert _job(client, "anything-at-all") == 401


def test_health_and_ready_stay_open(keyed, client):
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code not in (401, 403)


# ── startup ───────────────────────────────────────────────────────────────────────────


def test_startup_refused_with_no_keys(monkeypatch, caplog):
    import mori_advisor.ingestion_server as ing
    import mori_advisor.policy as pol

    monkeypatch.delenv("MORI_API_KEYS", raising=False)
    monkeypatch.delenv("MORI_ADVISOR_API_KEY", raising=False)
    monkeypatch.setattr(pol, "_TD_MODE", "api")
    with caplog.at_level(logging.CRITICAL), pytest.raises(SystemExit) as exc:
        ing.startup_check()
    assert "MORI_API_KEYS" in str(exc.value)


@pytest.mark.parametrize("mode", [None, "host"])
def test_startup_refused_in_host_mode_with_keys(monkeypatch, mode):
    import mori_advisor.ingestion_server as ing
    import mori_advisor.policy as pol

    monkeypatch.setenv("MORI_API_KEYS", f"{WRITE_KEY}:{WRITE_SECRET}")
    if mode is None:
        monkeypatch.delenv("MORI_TD_MODE", raising=False)
    else:
        monkeypatch.setenv("MORI_TD_MODE", mode)
    monkeypatch.setattr(pol, "_TD_MODE", "host")
    with pytest.raises(SystemExit) as exc:
        ing.startup_check()
    assert "MORI_TD_MODE" in str(exc.value)


def test_startup_allowed_with_keys_in_api_mode(monkeypatch):
    import mori_advisor.ingestion_server as ing
    import mori_advisor.policy as pol
    from mori_advisor.auth import hash_secret

    # v2.3.17: api mode needs hashed entries (a plain one refuses startup — test_v2317_keys.py)
    monkeypatch.setenv("MORI_API_KEYS", f"{WRITE_KEY}:{hash_secret(WRITE_SECRET)}")
    monkeypatch.setenv("MORI_API_KEY_ROLES", f"{WRITE_KEY}:write")
    monkeypatch.setattr(pol, "_TD_MODE", "api")
    ing.startup_check()  # no SystemExit


def test_the_entry_point_runs_the_startup_check():
    from pathlib import Path

    import mori_advisor.ingestion_server as ing

    src = Path(ing.__file__).read_text()
    main_block = src[src.index('if __name__ == "__main__":') :]
    assert "startup_check()" in main_block


# ── provenance: a dreamer key at this door is not canon authority ─────────────────────


def test_require_api_key_does_not_bind_the_actor(keyed, client):
    """The worker writes as `ingestion`; the door never sets current_actor, so no request-class
    authority can leak into the pipeline's writes."""
    from mori_advisor.policy import current_actor

    assert current_actor.get() is None
    assert _job(client, DREAM_SECRET) == 404
    assert current_actor.get() is None


@pytest.mark.parametrize("backend", ["sqlite"])
def test_an_ingested_canonical_result_is_a_proposal_even_under_a_dreamer_actor(
    backend, tmp_path, monkeypatch, keyed
):
    """Even if a dreamer identity were bound while the pipeline writes, ingestion's own provenance
    holds no canon authority: a canonical candidate (MORI_CURATE=false, the direct path) is queued."""
    from mori_advisor.policy import Actor, current_actor
    from tests.test_td_review import _make_pipeline, _run_with_backend

    monkeypatch.setenv("MORI_CURATE", "false")

    async def run(store):
        pipeline = _make_pipeline(store, monkeypatch)
        token = current_actor.set(Actor(DREAM_KEY, "dreamer"))
        try:
            await pipeline._write_memories(
                [
                    {
                        "name": "ing-canon",
                        "title": "t",
                        "description": "d",
                        "body": "b",
                        "tier": "canonical",
                        "confidence": 0.9,
                        "tags": [],
                    }
                ],
                tier="canonical",
                tags=[],
                source_uri="t.md",
                focus_mode="all",
            )
        finally:
            current_actor.reset(token)
        ms = store._mem if hasattr(store, "_mem") else store
        assert ms.get_memory("ing-canon") is None
        items = store.pending_list_json(status="pending")
        assert [i["name"] for i in items] == ["ing-canon"]

    _run_with_backend(backend, tmp_path, run)

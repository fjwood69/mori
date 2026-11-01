"""B1 characterisation (board ruling 2026-11-01): does a `decision` message, written the way msg_daemon writes it today,
reach a brief?

msg_daemon writes the message body straight through memory_store.write(type='project', tier='working', no tags,
actor='msg') with no human review. The board asked for a test, not an assumption, of whether such an untagged
working-tier row reaches a safe-scope brief. This file records the answer for every brief mode, so the B1 fix
(route through pending_writes) has a baseline it must change.
"""

from __future__ import annotations

import pytest

from tests.test_mcp_tools import BACKENDS, _run_with_backend, _seed_memory, _stub_brief_env

MARKER = "B1-decision-marker-7f3a"


async def _seed_like_msg_daemon(store):
    from mori_advisor.provenance import Provenance

    body = f"{MARKER}: switch the gateway to provider X (an agent's decision, unreviewed)"
    return await _seed_memory(
        store,
        name="b1-decision-from-msg",
        title=body[:80],
        body=body,
        type="project",
        tier="working",
        tags=[],
        client="peer-device",
        origin_clients=["peer-device"],
        provenance=Provenance(
            actor="msg", actor_detail="peer-device", source="msg_daemon", op="write"
        ),
    )


@pytest.mark.parametrize("backend", BACKENDS)
def test_reach_of_an_unreviewed_decision_in_every_brief_mode(backend, tmp_path, monkeypatch):
    import mori_advisor.main as m
    from mori_advisor.main import brief

    async def run(store):
        await _seed_memory(store, name="anchor-project-memory", tags=["project:mori"])
        await _seed_like_msg_daemon(store)
        _stub_brief_env(m, monkeypatch, store, tmp_path)
        reach = {
            "unscoped_safe": MARKER in await brief(project=None, scope="safe"),
            "project_safe": MARKER in await brief(project="mori", scope="safe"),
            "unscoped_all": MARKER in await brief(project=None, scope="all"),
            "project_all": MARKER in await brief(project="mori", scope="all"),
            "post_compact": MARKER in await brief(project=None, post_compact=True, since="1h"),
        }
        print("B1 REACH", reach)
        # Baseline as found on v2.3.12 (2026-11-01, commit 1445ffd): every scoped brief withheld it, but the
        # POST-COMPACT DELTA surfaced it (`post_compact: True`). B1 v2 flips that line: the delta now obeys the
        # brief's scope, so an untagged, unreviewed row reaches no brief mode.
        assert reach == {
            "unscoped_safe": False,
            "project_safe": False,
            "unscoped_all": False,
            "project_all": False,
            "post_compact": False,
        }

    _run_with_backend(backend, tmp_path, monkeypatch, run)

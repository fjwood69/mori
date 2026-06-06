"""mori-intake FastAPI application — ingest front door.

Mirrors the shape of mori_advisor/ingestion_server.py.
Auth: reuses mori_advisor.auth.check_key + mori_advisor.policy (same
MORI_API_KEYS / MORI_API_KEY_ROLES registry).

Open (no-auth) paths : GET /health, GET /ready
Protected paths      : POST /intake/submissions, GET /intake/candidates
Required role        : write (>= write in the ROLE_LEVELS hierarchy)
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import mori_intake.db as db
import mori_intake.worker as worker
from mori_advisor.auth import check_key, init_auth
from mori_advisor.policy import ROLE_LEVELS, role_for
from mori_intake import migrations
from mori_intake.config import WORKER_INTERVAL, check_data_boundary
from mori_intake.eligibility import evaluate as eligibility_evaluate
from mori_intake.ratelimit import get_limiter

logger = logging.getLogger(__name__)

# ── Auth helpers ──────────────────────────────────────────────────────────────


def _require_write(x_api_key: str | None) -> str:
    """Validate the API key and assert write role.

    Returns the client key_name on success.
    Raises HTTPException 401 / 403 on failure.
    """
    client_name = check_key(x_api_key)
    if client_name is None:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    if client_name != "anonymous":
        role = role_for(client_name)
        if ROLE_LEVELS.get(role, 0) < ROLE_LEVELS["write"]:
            raise HTTPException(
                status_code=403,
                detail=f"Key '{client_name}' has role '{role}' but 'write' is required",
            )
    return client_name


# ── Request / response models ─────────────────────────────────────────────────


class SubmissionRequest(BaseModel):
    session_id: str
    agent_id: str
    target: str  # "memory" | "user"
    action: str  # "add" | "replace" | "remove"
    stable_key: str
    content: str = ""
    provenance: dict[str, Any] | None = None


# ── Lifespan ──────────────────────────────────────────────────────────────────

_worker_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _worker_task

    # Config guard: refuse to start if intake DSN == mori DSN.
    check_data_boundary()

    # Initialise auth (loads MORI_API_KEYS / MORI_API_KEY_ROLES).
    init_auth()

    # Create the asyncpg pool.
    pool = await db.create_pool()

    # Run schema migrations (idempotent, advisory-locked).
    await migrations.apply(pool)

    # Start the drain worker.
    _worker_task = asyncio.create_task(worker.run_loop(pool, WORKER_INTERVAL))

    logger.info("mori-intake ready")
    yield

    # Shutdown — cancel the drain worker and close the pool.
    if _worker_task is not None:
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass

    await db.close_pool()
    logger.info("mori-intake shutdown complete")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="mori-intake", lifespan=lifespan, docs_url=None, redoc_url=None)


# ── Open endpoints ────────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    """200 iff the pool connects and migrations have been applied."""
    try:
        pool = db.get_pool()
        # Verify the pool is live and migrations are stamped.
        applied_count = await pool.fetchval("SELECT COUNT(*) FROM intake_schema_migrations")
        return {"status": "ok", "migrations_applied": applied_count}
    except Exception as exc:
        logger.warning("ready check failed: %s", exc)
        return JSONResponse(status_code=503, content={"status": "error", "detail": str(exc)})


# ── Protected endpoints ───────────────────────────────────────────────────────


@app.post("/intake/submissions", status_code=202)
async def post_submission(
    body: SubmissionRequest,
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
):
    """Accept an agent memory proposal.

    Validates the request, runs the eligibility gate, and inserts into
    ``intake_submissions``.  Returns 202 fast — no hashing, embedding, or
    dedup on the request path (those are the worker's job).

    Returns
    -------
    202 Accepted
        ``{ "status": "accepted", "submission_id": "<uuid>", "duplicate": false }``
        On idempotency hit (same session_id + stable_key):
        ``{ "status": "accepted", "submission_id": "<uuid>", "duplicate": true }``
    422 Unprocessable Entity
        ``{ "status": "rejected", "reason": "<eligibility reason>" }``
    400 Bad Request
        Missing required fields (handled by Pydantic validation).
    401 Unauthorized
        Bad or absent key.
    403 Forbidden
        Key role < write.
    """
    client_name = _require_write(x_api_key)

    # Rate-limit: POST /intake/submissions only, keyed on authenticated key name.
    limiter = get_limiter()
    verdict = limiter.check(client_name)
    if not verdict.allowed:
        return JSONResponse(
            status_code=429,
            content={"status": "rate_limited", "retry_after": verdict.retry_after},
            headers={"Retry-After": str(verdict.retry_after)},
        )

    # Validate required fields that Pydantic alone cannot enforce.
    if not body.session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    if not body.agent_id:
        raise HTTPException(status_code=400, detail="agent_id is required")
    if not body.target:
        raise HTTPException(status_code=400, detail="target is required")
    if not body.action:
        raise HTTPException(status_code=400, detail="action is required")
    if not body.stable_key:
        raise HTTPException(status_code=400, detail="stable_key is required")
    if body.action in ("add", "replace") and not body.content:
        raise HTTPException(status_code=400, detail="content is required for add/replace")

    # Eligibility gate.
    decision = eligibility_evaluate(
        target=body.target,
        action=body.action,
        stable_key=body.stable_key,
        body=body.content,
    )
    if not decision.eligible:
        return JSONResponse(
            status_code=422,
            content={"status": "rejected", "reason": decision.reason},
        )

    # GOV-008: derive the effective session_id server-side so that one agent
    # cannot collide with or submit proposals under another agent's session.
    # The agent-supplied body.session_id is used as a sub-scope within the
    # client's own namespace; the database key is ``{client_name}:{session_id}``.
    effective_session_id = f"{client_name}:{body.session_id}"

    # Insert into intake_submissions — idempotent on (effective_session_id, stable_key).
    pool = db.get_pool()

    provenance_json = json.dumps(body.provenance) if body.provenance is not None else None

    # Race-free idempotency: attempt the insert; on a (session_id, stable_key)
    # conflict the row already exists, so report a duplicate hit rather than
    # 500.  Avoids the TOCTOU window a separate SELECT-then-INSERT would open
    # under concurrent identical submissions (e.g. an outbox retry).
    # On conflict: return {duplicate:true} WITHOUT leaking the existing
    # submission_id to prevent session-namespace collisions from being used
    # to discover other agents' submission IDs (GOV-008).
    new_id = await pool.fetchval(
        "INSERT INTO intake_submissions "
        "  (id, session_id, agent_id, target_name, action, stable_key, "
        "   raw_source_text, provenance) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb) "
        "ON CONFLICT (session_id, stable_key) DO NOTHING "
        "RETURNING id",
        uuid.uuid4(),
        effective_session_id,
        body.agent_id,
        body.target,
        body.action,
        body.stable_key,
        body.content,
        provenance_json,
    )

    if new_id is None:
        # Conflict — the (effective_session_id, stable_key) row already exists.
        # Do NOT return the existing submission_id: leaking it would allow one
        # agent to probe another agent's session namespace (GOV-008).
        return {
            "status": "accepted",
            "duplicate": True,
        }

    return {
        "status": "accepted",
        "submission_id": str(new_id),
        "duplicate": False,
    }


@app.get("/intake/candidates")
async def get_candidates(
    status: str = Query(default="pending"),
    limit: int = Query(default=50, ge=1, le=500),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
):
    """Return intake candidates for operator inspection.

    Read-only; for dreamer / operator verification — no agent-facing read API
    by design (agents read mori canon, not intake).

    Query parameters
    ----------------
    status : str
        Filter by candidate status (default ``pending``).
    limit : int
        Maximum number of rows to return (default 50, max 500).
    """
    _require_write(x_api_key)

    pool = db.get_pool()
    rows = await pool.fetch(
        "SELECT id, canonicalized_body, content_hash, status, "
        "       reinforcement_count, created_at, promoted_canon_name "
        "FROM intake_candidates "
        "WHERE status = $1 "
        "ORDER BY created_at DESC "
        "LIMIT $2",
        status,
        limit,
    )

    return [
        {
            "id": str(r["id"]),
            "canonicalized_body": r["canonicalized_body"],
            "content_hash": r["content_hash"],
            "status": r["status"],
            "reinforcement_count": r["reinforcement_count"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "promoted_canon_name": r["promoted_canon_name"],
        }
        for r in rows
    ]

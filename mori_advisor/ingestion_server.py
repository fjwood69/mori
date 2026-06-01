"""Mori ingestion server — dedicated HTTP upload endpoint for client-side files.

Runs on port 8969 (MORI_INGESTION_PORT). Same image as mori-advisor, different
entrypoint: python -m mori_advisor.ingestion_server

Accepts multipart file uploads, queues them as async jobs, and processes them
through the shared IngestionPipeline. Both pods share memories.db via SQLite WAL.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from starlette.requests import Request

from mori_advisor.bifrost_client import BifrostClient
from mori_advisor.ingestion import IngestionPipeline
from mori_advisor.memory_store import MemoryStore
from mori_advisor.store import get_store

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────

DATA_DIR = Path(os.environ.get("MORI_ADVISOR_DATA", "/data/mori-advisor"))
INGESTION_PORT = int(os.environ.get("MORI_INGESTION_PORT", "8969"))
API_KEY = os.environ.get("MORI_ADVISOR_API_KEY", "")
BIFROST_BASE_URL = os.environ.get("MORI_BASE_URL", "http://localhost:8787")
BIFROST_TIMEOUT = int(os.environ.get("MORI_BIFROST_TIMEOUT", "300"))
MAX_FILE_MB = int(os.environ.get("MORI_INGESTION_MAX_FILE_MB", "50"))
MAX_TOTAL_MB = MAX_FILE_MB * 4

# ── Models ────────────────────────────────────────────────────────────────


@dataclass
class _UploadedFile:
    name: str
    content_b64: str
    mime_type: str


@dataclass
class JobStatus:
    job_id: str
    status: str  # queued|running|complete|failed
    files: list[str]
    params: dict
    memories_written: int = 0
    errors: list[str] = field(default_factory=list)
    started_at: str | None = None
    completed_at: str | None = None


# ── Global state ──────────────────────────────────────────────────────────

store = get_store(DATA_DIR / "memories.db")
store.bootstrap()
bifrost = BifrostClient(base_url=BIFROST_BASE_URL, timeout=BIFROST_TIMEOUT)
memory_store = MemoryStore(db_path=DATA_DIR / "memories.db")
pipeline = IngestionPipeline(
    db_path=DATA_DIR / "memories.db",
    bifrost_client=bifrost,
    memory_store=memory_store,
)
jobs: dict[str, JobStatus] = {}
job_queue: asyncio.Queue  # initialised in lifespan


# ── Auth ──────────────────────────────────────────────────────────────────


def require_api_key(request: Request) -> None:
    if API_KEY and request.headers.get("X-Api-Key", "") != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ── Worker ────────────────────────────────────────────────────────────────


async def _worker() -> None:
    while True:
        job_id, uploaded = await job_queue.get()
        job = jobs[job_id]
        logger.info("ingestion job %s starting: %s", job_id, job.files)
        job.status = "running"
        job.started_at = datetime.now(timezone.utc).isoformat()
        try:
            parsed_tags = [t.strip() for t in job.params["tags"].split(",") if t.strip()]
            result = await asyncio.to_thread(
                pipeline.ingest_content,
                files=[
                    {"name": u.name, "content_b64": u.content_b64, "mime_type": u.mime_type}
                    for u in uploaded
                ],
                focus=job.params["focus"],
                tier=job.params["tier"],
                tags=parsed_tags,
                dry_run=job.params["dry_run"],
                force=job.params["force"],
            )
            job.status = "complete"
            job.memories_written = result.get("memories_written", 0)
            if result.get("error_details"):
                job.errors = result["error_details"][:10]
        except Exception as e:
            logger.error("Ingestion job %s failed: %s", job_id, e)
            job.status = "failed"
            job.errors = [str(e)]
        finally:
            job.completed_at = datetime.now(timezone.utc).isoformat()
            job_queue.task_done()


# ── App ───────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    global job_queue
    store.ping()
    job_queue = asyncio.Queue()
    asyncio.create_task(_worker())
    yield


app = FastAPI(title="mori-ingestion", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "mori-ingestion"}


@app.get("/ready")
async def ready():
    try:
        store.ping()
        return {"status": "ok"}
    except Exception as e:
        return JSONResponse(status_code=503, content={"status": "error", "detail": str(e)})


@app.post("/api/ingest/upload", status_code=202, dependencies=[Depends(require_api_key)])
async def upload(
    files: list[UploadFile] = File(...),
    focus: str = Form("all"),
    tier: str = Form("working"),
    tags: str = Form(""),
    dry_run: bool = Form(False),
    force: bool = Form(False),
):
    if tier not in ("working", "canonical", "ephemeral"):
        raise HTTPException(status_code=422, detail=f"Invalid tier '{tier}'")

    max_file_bytes = MAX_FILE_MB * 1024 * 1024
    max_total_bytes = MAX_TOTAL_MB * 1024 * 1024
    uploaded: list[_UploadedFile] = []
    total_bytes = 0

    for f in files:
        data = await f.read()
        if len(data) > max_file_bytes:
            raise HTTPException(
                status_code=413, detail=f"{f.filename}: exceeds {MAX_FILE_MB}MB limit"
            )
        total_bytes += len(data)
        if total_bytes > max_total_bytes:
            raise HTTPException(status_code=413, detail=f"Total upload exceeds {MAX_TOTAL_MB}MB")
        uploaded.append(
            _UploadedFile(
                name=f.filename or "upload",
                content_b64=base64.b64encode(data).decode("ascii"),
                mime_type=f.content_type or "application/octet-stream",
            )
        )

    job_id = str(uuid.uuid4())
    jobs[job_id] = JobStatus(
        job_id=job_id,
        status="queued",
        files=[u.name for u in uploaded],
        params={
            "focus": focus,
            "tier": tier,
            "tags": tags,
            "dry_run": dry_run,
            "force": force,
        },
    )
    logger.info("ingestion job %s queued: %s", job_id, [u.name for u in uploaded])
    await job_queue.put((job_id, uploaded))

    return {"job_id": job_id, "status": "queued", "files": [u.name for u in uploaded]}


@app.get("/api/ingest/job/{job_id}", dependencies=[Depends(require_api_key)])
async def get_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "job_id": job.job_id,
        "status": job.status,
        "files": job.files,
        "memories_written": job.memories_written,
        "errors": job.errors,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
    }


@app.get("/api/ingest/status", dependencies=[Depends(require_api_key)])
async def ingest_status():
    return IngestionPipeline.get_status(DATA_DIR / "memories.db", limit=20)


# ── Entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=INGESTION_PORT)

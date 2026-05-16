"""Dream job entry point for scheduled execution.

Invoked by systemd timer (GCE) or cron (homelab). This is a standalone
script that runs the dream pipeline once and exits — separate from the
long-running MCP server.

Usage:
    python -m moku_advisor.dream_job
    # or from the container:
    podman exec moku-advisor python -m moku_advisor.dream_job
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path


class _GCPJsonFormatter(logging.Formatter):
    """Structured JSON log formatter for GCP Cloud Logging."""

    def format(self, record):
        return json.dumps({
            "severity": record.levelname,
            "message": record.getMessage(),
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%fZ"),
        })


DATA_DIR = Path(os.environ.get("MOKU_ADVISOR_DATA", "/data/moku-advisor"))
DB_PATH = DATA_DIR / "memories.db"

if os.environ.get("GCE_METADATA_HOST"):
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(_GCPJsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[_handler])
else:
    logging.basicConfig(level=logging.INFO)

logger = logging.getLogger("dream-job")


def main():
    from moku_advisor.bifrost_client import BifrostClient
    from moku_advisor.dream import DreamPipeline

    bifrost = BifrostClient()
    pipeline = DreamPipeline(
        db_path=DB_PATH,
        bifrost_client=bifrost,
        trusted_dreamers=(
            os.environ.get("MOKU_TRUSTED_DREAMERS", "").split(",")
            if os.environ.get("MOKU_TRUSTED_DREAMERS") else []
        ),
        nats_url=os.environ.get("MOKU_NATS_URL") or None,
    )

    logger.info("Dream job started")
    status_before = pipeline.get_status()
    logger.info("Pre-run status: %s", status_before.replace("\n", " | "))

    try:
        memories = pipeline.run(dry_run=False)
        if memories:
            logger.info("Dream complete: %s memories written", len(memories))
        else:
            logger.info("No new memories to write")
    except Exception as e:
        logger.error("Dream job failed: %s", e, exc_info=True)
        sys.exit(1)

    status_after = pipeline.get_status()
    logger.info("Post-run status: %s", status_after.replace("\n", " | "))
    logger.info("Dream job finished")


if __name__ == "__main__":
    main()

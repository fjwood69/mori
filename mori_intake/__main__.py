"""Entry point: python -m mori_intake

Starts the mori-intake service on MORI_INTAKE_PORT (default 8971).
"""

from __future__ import annotations

import logging

import uvicorn

from mori_intake.config import INTAKE_PORT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

if __name__ == "__main__":
    uvicorn.run(
        "mori_intake.app:app",
        host="0.0.0.0",
        port=INTAKE_PORT,
        log_level="info",
    )

"""Hafif health servisi — Railway healthcheck. Worker loop YOK (cron ayrı).

Railway: uvicorn app.health:app --host 0.0.0.0 --port $PORT
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.cron._logging import setup_logging

setup_logging()
app = FastAPI(title="Rosso Worker Health", version="1.0.0")


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "rosso-worker"}


@app.get("/health/ready")
def health_ready() -> JSONResponse:
    try:
        from app.db import get_client
        get_client().table("export_jobs").select("id").limit(1).execute()
        return JSONResponse({"status": "ready", "db": "ok"})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"status": "not_ready", "db": str(e)}, status_code=503)


# Internal refresh router — Next.js'ten anlık tazeleme (FAZ 2, X-Worker-Secret korumalı)
try:
    from app.routers.internal_router import router as internal_router
    app.include_router(internal_router)
except Exception as e:  # noqa: BLE001
    logging.getLogger("rosso.worker").error("Internal Router yüklenemedi: %s", e)

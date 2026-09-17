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
#
# ⚠ Fail-closed startup guard (2026-09 audit): WORKER_SHARED_SECRET tanımsızsa
# ve açık `WORKER_LOCAL_DEV=1` opt-in'i de yoksa, router'ı HİÇ mount ETMEYİZ.
# Eskiden secret boşsa router'daki kontrol kendisi sessizce atlanıyordu — yani
# env var deploy'da eksik kalırsa (yanlış panel, unutulmuş secret) prod'da
# /internal/refresh (gerçek Spotify sync + DB yazımı tetikler) kimliksiz açık
# kalıyordu. Artık: secret yok + dev opt-in yok → CRITICAL log + router yok
# (endpoint 404 döner, "sessizce açık" yerine "görünür biçimde kapalı").
try:
    from app.routers.internal_router import router as internal_router
    from app.services.worker_auth import is_local_dev_mode, worker_secret_configured

    _health_logger = logging.getLogger("rosso.worker")
    if worker_secret_configured() or is_local_dev_mode():
        app.include_router(internal_router)
    else:
        _health_logger.critical(
            "WORKER_SHARED_SECRET tanımsız ve WORKER_LOCAL_DEV=1 opt-in'i yok — "
            "internal_router (/internal/refresh) FAIL-CLOSED olarak mount EDİLMEDİ. "
            "Prod'da bu bir deploy hatasıdır: WORKER_SHARED_SECRET set edin."
        )
except Exception as e:  # noqa: BLE001
    logging.getLogger("rosso.worker").error("Internal Router yüklenemedi: %s", e)

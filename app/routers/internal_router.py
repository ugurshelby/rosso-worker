"""Internal refresh router — Next.js'ten tek kullanıcı için anlık tazeleme (FAZ 2).

Kullanıcı siteye girince Next.js buraya POST atar; worker o kullanıcının
recently-played ve/veya playlist verisini HEMEN tazeler. Cron'lar sessiz
güvenlik ağı olarak kalır (kullanıcı hiç girmese de veri akar).

Güvenlik: ytmusic_router ile AYNI model — X-Worker-Secret header'ı, hmac
sabit-zamanlı karşılaştırma. WORKER_SHARED_SECRET tanımlı değilse ve açık
`WORKER_LOCAL_DEV=1` opt-in'i de yoksa istek FAIL-CLOSED reddedilir (503) —
ayrıca `app/health.py` başlangıçta bu router'ı hiç mount etmez (bkz. orada
CRITICAL log). Eskiden secret boşsa kontrol tamamen atlanıyordu (fail-open);
2026-09 audit bulgusu üzerine değiştirildi (bkz. `app/services/worker_auth.py`).

Mantık KOPYALANMAZ: cron'ların çağırdığı aynı runner'lar user_id ile çağrılır.
"""
from __future__ import annotations

import hmac
import logging
import os
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from app.db import get_client
from app.pipeline.playlist_refresh_runner import run_one_playlist_refresh
from app.pipeline.recently_played_runner import run_one_recently_played_sync
from app.services.worker_auth import is_local_dev_mode

logger = logging.getLogger("rosso.worker.internal_router")

# Anlık tazeleme debounce: son sync bu süreden yeniyse iş yapmadan skipped dön.
_DEBOUNCE_SECONDS = 120


def require_worker_secret(x_worker_secret: str | None = Header(default=None)) -> None:
    expected = os.environ.get("WORKER_SHARED_SECRET", "")
    if not expected:
        if is_local_dev_mode():
            return  # açık yerel-dev opt-in: kontrol atlanır
        # Secret yok VE dev opt-in de yok → fail-closed. Prod'da bu satıra
        # normalde hiç gelinmez (health.py router'ı zaten mount etmez), ama
        # savunma-derinliği için burada da reddediyoruz.
        logger.critical(
            "WORKER_SHARED_SECRET tanımsız ve WORKER_LOCAL_DEV=1 opt-in'i yok — "
            "/internal/refresh isteği reddedildi (fail-closed)."
        )
        raise HTTPException(status_code=503, detail="worker_misconfigured")
    if not x_worker_secret or not hmac.compare_digest(x_worker_secret, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


router = APIRouter(
    prefix="/internal", tags=["internal"], dependencies=[Depends(require_worker_secret)]
)


class RefreshBody(BaseModel):
    user_id: str
    kind: Literal["recently_played", "playlists", "both"] = "both"


def _crypto_key() -> str:
    from app.config import get_settings
    return getattr(get_settings(), "token_encryption_key", "") or ""


def _recently_played_debounced(client: Any, user_id: str) -> bool:
    """Son recently-played sync < 2 dk ise True (atla) döner."""
    res = (
        client.table("platform_connections")
        .select("last_recently_played_sync_at")
        .eq("platform", "spotify")
        .eq("is_active", True)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    if not rows:
        return False
    stamp = rows[0].get("last_recently_played_sync_at")
    if not stamp:
        return False
    try:
        last = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False
    age = (datetime.now(timezone.utc) - last).total_seconds()
    return age < _DEBOUNCE_SECONDS


@router.post("/refresh")
def internal_refresh(body: RefreshBody) -> dict[str, Any]:
    """Tek kullanıcı için anlık tazeleme. Cron runner'larını user_id ile çağırır."""
    client = get_client()
    crypto_key = _crypto_key()
    out: dict[str, Any] = {"user_id": body.user_id, "kind": body.kind}

    with httpx.Client(timeout=15) as http:
        if body.kind in ("recently_played", "both"):
            if _recently_played_debounced(client, body.user_id):
                out["recently_played"] = {"skipped": True, "reason": "debounce"}
            else:
                out["recently_played"] = run_one_recently_played_sync(
                    client, http, crypto_key, user_id=body.user_id
                )

        if body.kind in ("playlists", "both"):
            out["playlists"] = run_one_playlist_refresh(
                client, http, crypto_key, user_id=body.user_id
            )

    logger.info('{"internal_refresh":"%s","kind":"%s"}', body.user_id, body.kind)
    return out

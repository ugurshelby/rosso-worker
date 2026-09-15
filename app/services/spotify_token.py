"""Worker-tarafı Spotify access token okuma + otomatik refresh.

TS tarafındaki ensureValidToken (src/lib/services/token-refresh.ts) desenin
Python karşılığı — worker cron'ları TS'ye bağımlı olmadan bağımsız çalışır.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from app.services.token_cipher import decrypt_token, encrypt_token

logger = logging.getLogger("rosso.worker.spotify_token")

_REFRESH_THRESHOLD = timedelta(minutes=5)
_TOKEN_URL = "https://accounts.spotify.com/api/token"


def get_valid_spotify_token(
    client: Any, user_id: str, crypto_key: str, http: Any, force_refresh: bool = False
) -> str | None:
    """Kullanıcının geçerli Spotify access token'ını döner; gerekirse yeniler.
    
    force_refresh=True ise süresi dolmamış olsa bile yenileme zorlanır.
    Bağlantı is_active=False kalmış olsa dahi force_refresh ile canlandırma (resurrect) denenir.
    """
    res = (
        client.table("platform_connections")
        .select("access_token, refresh_token, token_expires, is_active")
        .eq("user_id", user_id)
        .eq("platform", "spotify")
        .execute()
    )
    rows = res.data or []
    if not rows or not rows[0].get("access_token"):
        return None

    conn = rows[0]
    # force_refresh değilse ve bağlantı pasifse devam etme
    if not conn.get("is_active") and not force_refresh:
        return None

    expires_raw = conn.get("token_expires")
    needs_refresh = force_refresh
    if not needs_refresh and expires_raw:
        try:
            expires = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
            needs_refresh = (expires - datetime.now(timezone.utc)) < _REFRESH_THRESHOLD
        except (ValueError, AttributeError):
            needs_refresh = False

    if not needs_refresh:
        return decrypt_token(conn["access_token"], crypto_key)

    refresh_token = decrypt_token(conn.get("refresh_token", ""), crypto_key)
    if not refresh_token:
        return None

    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "")

    try:
        resp = http.post(
            _TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            auth=(client_id, client_secret),
            timeout=10,
        )
        if resp.status_code == 400:
            err_body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            if err_body.get("error") == "invalid_grant":
                logger.error("Spotify refresh_token geçersiz (invalid_grant) — is_active=False: user=%s", user_id)
                try:
                    client.table("platform_connections").update({"is_active": False}).eq("user_id", user_id).eq("platform", "spotify").execute()
                except Exception:  # noqa: BLE001
                    pass
                return None
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Spotify token refresh başarısız: user=%s, exc=%s", user_id, exc)
        return None

    new_access = payload.get("access_token")
    if not new_access:
        return None

    expires_in = payload.get("expires_in", 3600)
    new_expires = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
    update_payload = {
        "access_token": encrypt_token(new_access, crypto_key),
        "token_expires": new_expires,
        "is_active": True,
    }
    # Spotify bazen yeni refresh_token döner — varsa üzerine yazılmalı (eskisi geçersiz kalabilir)
    if payload.get("refresh_token"):
        update_payload["refresh_token"] = encrypt_token(payload["refresh_token"], crypto_key)

    try:
        client.table("platform_connections").update(update_payload).eq(
            "user_id", user_id
        ).eq("platform", "spotify").execute()
    except Exception:  # noqa: BLE001
        logger.warning("Spotify token DB güncelleme başarısız: user=%s", user_id)

    return new_access

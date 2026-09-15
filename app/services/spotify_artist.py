"""Spotify artist-tabanlı genre çekimi — ISRC gerektirmez (spec §5).

İki adım:
  1. get_track_artist_ids: /tracks/{id} → artist ID listesi (export sonrası eksik kalanlar için)
  2. get_artist_genres:    /artists/{id} → genre listesi

Dev Mode kısıtı: batch (?ids=) 403 → tekil path endpoint kullanılır.
Token/retry/circuit-breaker spotify_lookup'tan yeniden kullanılır (DRY).
Kota tükenince (SpotifyQuotaExhausted) o ana dek toplanan + quota_hit=True döner.
"""
from __future__ import annotations

import logging
import os as _os
import time
from typing import Any

from app.services.spotify_lookup import (
    _get_access_token,
    _with_retry,
    SpotifyQuotaExhausted,
)

logger = logging.getLogger("rosso.worker.spotify_artist")

_LOOKUP_DELAY_S = float(_os.environ.get("SPOTIFY_ARTIST_DELAY_S", "1.5"))


def _fetch_each(
    ids: list[str],
    url_tmpl: str,
    extract: Any,
    client_id: str,
    client_secret: str,
    http_client: Any,
) -> tuple[dict[str, Any], bool]:
    """Tekil path endpoint'leri sırayla çek; quota_hit'te döngüyü kır.

    extract(item) → (key, value) veya None döndürür.
    """
    if not ids:
        return {}, False

    token = _get_access_token(client_id, client_secret, http_client)
    if not token:
        logger.warning("Spotify token alınamadı — lookup atlanıyor")
        return {}, False

    results: dict[str, Any] = {}
    headers = {"Authorization": f"Bearer {token}"}
    quota_hit = False

    for idx, _id in enumerate(ids):
        def _do_get(_id: str = _id) -> Any:
            resp = http_client.get(url_tmpl.format(id=_id), headers=headers, timeout=10)
            resp.raise_for_status()
            return resp

        try:
            resp = _with_retry(_do_get)
        except SpotifyQuotaExhausted:
            quota_hit = True
            logger.warning("Spotify kotası tükendi — %d kayıt toplandı", len(results))
            break
        except Exception:  # noqa: BLE001 — tek kayıt hatası (404 vb.) diğerlerini etkilemez
            logger.warning("Spotify lookup başarısız: %s", _id)
            resp = None

        if resp is not None:
            try:
                kv = extract(resp.json())
                if kv is not None:
                    results[kv[0]] = kv[1]
            except Exception:  # noqa: BLE001
                logger.warning("Spotify yanıtı çözümlenemedi: %s", _id)

        if idx < len(ids) - 1:
            time.sleep(_LOOKUP_DELAY_S)

    return results, quota_hit


def get_track_artist_ids(
    spotify_ids: list[str],
    client_id: str,
    client_secret: str,
    http_client: Any,
) -> tuple[dict[str, list[str]], bool]:
    """{track_spotify_id: [artist_id,...]}, quota_hit döndür."""
    def _extract(item: dict[str, Any]) -> tuple[str, list[str]] | None:
        tid = item.get("id")
        if not tid:
            return None
        artist_ids = [a["id"] for a in item.get("artists", []) if a.get("id")]
        return tid, artist_ids

    return _fetch_each(
        spotify_ids, "https://api.spotify.com/v1/tracks/{id}",
        _extract, client_id, client_secret, http_client,
    )


def get_artist_genres(
    artist_ids: list[str],
    client_id: str,
    client_secret: str,
    http_client: Any,
) -> tuple[dict[str, list[str]], bool]:
    """{artist_id: [genre,...]}, quota_hit döndür."""
    def _extract(item: dict[str, Any]) -> tuple[str, list[str]] | None:
        aid = item.get("id")
        if not aid:
            return None
        return aid, item.get("genres", [])

    return _fetch_each(
        artist_ids, "https://api.spotify.com/v1/artists/{id}",
        _extract, client_id, client_secret, http_client,
    )

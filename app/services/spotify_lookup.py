"""Spotify track lookup servisi — G.2 kısmi eşleştirme için.

raw_track_name + raw_artist_name → tracks tablosuna ISRC + metadata yaz.
Development Mode kısıtlaması altında /search ve /tracks endpoint'leri açık.

Rate limit: Spotify ~180 istek/dk (Development) — token başına.
G.3: exponential backoff retry (stdlib) — httpx pool process_export'ta yönetilir.
"""
from __future__ import annotations

import base64
import logging
import time
from typing import Any

# G.3: Retry ayarları — env ile override edilebilir
import os as _os
_MAX_RETRIES = int(_os.environ.get("SPOTIFY_LOOKUP_MAX_RETRIES", "3"))
_RETRY_BASE_DELAY = float(_os.environ.get("SPOTIFY_LOOKUP_RETRY_BASE_DELAY", "1.0"))
# 429 Retry-After üst sınırı (saniye). Spotify bazen absürt değerler döndürür
# (gerçek olay 2026-06-22: Retry-After=70771 ≈ 19.6 saat → worker saatlerce bloke).
# Cap'i aşan bekleme talebinde retry'dan vazgeçilir; track_id=None ile devam edilir
# (CLAUDE.md §3: event silinmez; §2: tek track tüm job'u kilitlemez).
_MAX_RETRY_AFTER = float(_os.environ.get("SPOTIFY_LOOKUP_MAX_RETRY_AFTER", "60.0"))


class SpotifyQuotaExhausted(Exception):
    """Spotify kotası tükendi (cap'i aşan Retry-After ile 429).

    Bu, normal "track bulunamadı" (None) durumundan ayrıdır. Çağıran (process_export)
    bunu yakalayıp circuit breaker açar: o job boyunca Spotify lookup'ı tamamen bırakır,
    böylece binlerce boşuna istek + HTTP/2 bağlantı çöküşü (last_stream_id) önlenir.

    `retry_after`: Spotify'ın istediği bekleme (saniye), biliniyorsa. Cooldown
    damgası bu değerle vurulmalı — sabit bir süre uydurmak cezayı besler
    (2026-08-01: kapak dolgusu Retry-After=64926s'ye karşılık sabit 3600s
    yazıyordu; her saat uyanıp yeni 429 yiyordu). HTTP/2 kopmasında süre
    bilinmez → None; çağıran §4.2 gereği 1 saat varsayar.
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _with_retry(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Exponential backoff retry (429 + 5xx için). Diğer hataları hemen re-raise eder."""
    import httpx
    delay = _RETRY_BASE_DELAY
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except httpx.RemoteProtocolError as exc:
            # HTTP/2 ConnectionTerminated: Spotify kota bitince bağlantıyı kesiyor.
            # Bu durumu SpotifyQuotaExhausted'a çevir → circuit-breaker devreye girer,
            # job çökmez, o ana kadar toplanan track'ler korunur.
            logger.warning("Spotify HTTP/2 bağlantısı kesildi — kota tükendi sinyali: %s", exc)
            raise SpotifyQuotaExhausted(f"HTTP/2 ConnectionTerminated: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 429:
                retry_after = float(exc.response.headers.get("Retry-After", delay))
                if retry_after > _MAX_RETRY_AFTER:
                    logger.warning(
                        "Spotify 429 Retry-After=%ss cap'i (%ss) aşıyor — kota tükendi, lookup bırakılıyor",
                        retry_after, _MAX_RETRY_AFTER,
                    )
                    raise SpotifyQuotaExhausted(
                        f"Retry-After={retry_after}s > cap={_MAX_RETRY_AFTER}s",
                        retry_after=retry_after,
                    ) from exc
                logger.warning("Spotify 429 rate-limit, %ss bekleniyor (deneme %d)", retry_after, attempt)
                time.sleep(retry_after)
            elif status >= 500:
                logger.warning("Spotify %d sunucu hatası, %ss bekleniyor (deneme %d)", status, delay, attempt)
                time.sleep(delay)
                delay *= 2
            else:
                raise  # 4xx (401/404 vb.) — retry anlamsız
        except Exception:
            if attempt == _MAX_RETRIES:
                raise
            logger.warning("Spotify isteği başarısız (deneme %d), %ss bekleniyor", attempt, delay)
            time.sleep(delay)
            delay *= 2
    return None  # tüm denemeler tükendi

logger = logging.getLogger("rosso.worker.spotify_lookup")

# Spotify app token önbelleği (servis ömrü boyunca) — client_id BAŞINA.
# 2026-09-23: eskiden tek küresel önbellekti. Katalog bakımı artık her
# kullanıcının KENDİ app'iyle çalışıyor (`spotify_kimlik_havuzu`); tek önbellek
# bir app'in token'ını ötekine verirdi (yanlış app kotasına yazılan istek).
_token_cache: dict[str, dict[str, Any]] = {}


def _get_access_token(client_id: str, client_secret: str, http_client: Any) -> str | None:
    """Client Credentials flow ile Spotify access token al (client_id başına cache'li)."""
    now = time.time()
    onbellek = _token_cache.get(client_id)
    if onbellek and onbellek["access_token"] and now < onbellek["expires_at"] - 30:
        return onbellek["access_token"]  # type: ignore[return-value]

    try:
        creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

        def _do_token() -> Any:
            r = http_client.post(
                "https://accounts.spotify.com/api/token",
                headers={"Authorization": f"Basic {creds}"},
                data={"grant_type": "client_credentials"},
                timeout=10,
            )
            r.raise_for_status()
            return r

        resp = _with_retry(_do_token)
        if resp is None:
            logger.warning("Spotify token alınamadı (tüm denemeler tükendi)")
            return None
        data = resp.json()
        _token_cache[client_id] = {
            "access_token": data["access_token"],
            "expires_at": now + data.get("expires_in", 3600),
        }
        logger.info("Spotify access token alındı (geçerlilik: %ds)", data.get("expires_in", 3600))
        return _token_cache[client_id]["access_token"]  # type: ignore[return-value]
    except Exception:  # noqa: BLE001
        logger.warning("Spotify token alınamadı")
        return None


def upsert_track(track_data: dict[str, Any], supabase_client: Any) -> str | None:
    """tracks tablosuna kayıt ekle veya güncelle; satır ID'sini döndür.

    ISRC = tek doğru kaynak (CLAUDE.md §3). spotify_id üzerinden upsert.
    """
    payload: dict[str, Any] = {
        "spotify_id": track_data["spotify_id"],
        "title": track_data["title"],
        "artists": track_data["artists"],
    }
    if track_data.get("isrc"):
        payload["isrc"] = track_data["isrc"]
    if track_data.get("duration_ms"):
        payload["duration_ms"] = track_data["duration_ms"]
    if track_data.get("album"):
        payload["album"] = track_data["album"]
    if track_data.get("album_image_url"):
        payload["album_image_url"] = track_data["album_image_url"]

    try:
        res = (
            supabase_client.table("tracks")
            .upsert(payload, on_conflict="spotify_id")
            .execute()
        )
        rows = res.data or []
        return rows[0]["id"] if rows else None
    except Exception:  # noqa: BLE001
        logger.warning("Track upsert başarısız: spotify_id=%s", track_data.get("spotify_id"))
        return None

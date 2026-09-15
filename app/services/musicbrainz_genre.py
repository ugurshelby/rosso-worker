"""MusicBrainz tür çekimi — recording (şarkı) ve artist (sanatçı) düzeyinde.

Deezer + Last.fm'in yanında üçüncü kaynak. MB verisi küratörlü (topluluk
etiketli) ve 'genres' alanı temiz kanonik türler içerir; boşsa serbest 'tags'
alanına düşülür.

Akış (canlı test 2026-07-01 ile doğrulandı):
  recording: /recording?query=artist:"X" AND recording:"Y" → MBID
             → /recording/{mbid}?inc=genres+tags → genres/tags
  artist:    /artist?query=artist:"X" → MBID
             → /artist/{mbid}?inc=genres+tags → genres/tags

Rate limit: anonim 1 req/sn. Her istekten önce _RATE_LIMIT_SLEEP kadar beklenir.
User-Agent header ZORUNLU (MB kuralı; yoksa 403).
"""
from __future__ import annotations

import logging
import threading
import time
import urllib.parse
from typing import Any

logger = logging.getLogger("rosso.worker.musicbrainz_genre")

_BASE = "https://musicbrainz.org/ws/2"
_USER_AGENT = "Rosso/1.0 (holocraftai@gmail.com)"
_HEADERS = {"User-Agent": _USER_AGENT}
_RATE_LIMIT_SLEEP = 1.1  # saniye; MB anonim limiti 1 req/sn (test'te 0'a çekilir)
_TAG_LIMIT = 8

# Process-genelinde MB seri kilit: 8 paralel thread olsa da MB çağrıları tek
# sırada, ≥_RATE_LIMIT_SLEEP aralıkla gider (1 req/sn kuralı garanti).
_MB_LOCK = threading.Lock()
_last_request_ts = 0.0  # monotonik saniye — son MB isteği zamanı


def _reset_rate_limit_state() -> None:
    """Test yardımcısı: son-istek zaman damgasını sıfırla."""
    global _last_request_ts
    _last_request_ts = 0.0


def _genres_or_tags(payload: Any) -> list[tuple[str, int]]:
    """Lookup yanıtından (name, count) çiftleri; 'genres' öncelikli, boşsa 'tags'."""
    data = payload or {}
    items = data.get("genres") or []
    if not items:
        items = data.get("tags") or []
    result: list[tuple[str, int]] = []
    for it in items:
        if isinstance(it, dict) and it.get("name"):
            result.append((it["name"], int(it.get("count") or 0)))
    return result[:_TAG_LIMIT]


def _get(http_client: Any, url: str) -> Any:
    """MB isteği: process-genelinde seri kilit (≥1 req/sn) + User-Agent header. Hata → None."""
    global _last_request_ts
    with _MB_LOCK:
        if _RATE_LIMIT_SLEEP:
            now = time.monotonic()
            wait = _RATE_LIMIT_SLEEP - (now - _last_request_ts)
            if wait > 0:
                time.sleep(wait)
        try:
            resp = http_client.get(url, timeout=10, headers=_HEADERS)
            resp.raise_for_status()
            result = resp.json()
        except Exception:  # noqa: BLE001
            logger.warning("MusicBrainz istek başarısız: %s", url)
            result = None
        finally:
            _last_request_ts = time.monotonic()
    return result


def get_musicbrainz_recording_genres(
    artist: str, title: str, http_client: Any
) -> list[tuple[str, int]]:
    """artist + title → MB recording türleri (name, count). Bulunamazsa []."""
    if not artist or not title:
        return []

    query = urllib.parse.quote(f'artist:"{artist}" AND recording:"{title}"')
    search = _get(http_client, f"{_BASE}/recording?query={query}&limit=1&fmt=json")
    if not search:
        return []
    recordings = search.get("recordings") or []
    if not recordings:
        return []
    mbid = recordings[0].get("id")
    if not mbid:
        return []

    lookup = _get(http_client, f"{_BASE}/recording/{mbid}?inc=genres+tags&fmt=json")
    if not lookup:
        return []
    return _genres_or_tags(lookup)


def get_musicbrainz_artist_genres(
    artist: str, http_client: Any
) -> list[tuple[str, int]]:
    """artist → MB artist türleri (name, count). Bulunamazsa []."""
    if not artist:
        return []

    query = urllib.parse.quote(f'artist:"{artist}"')
    search = _get(http_client, f"{_BASE}/artist?query={query}&limit=1&fmt=json")
    if not search:
        return []
    artists = search.get("artists") or []
    if not artists:
        return []
    mbid = artists[0].get("id")
    if not mbid:
        return []

    lookup = _get(http_client, f"{_BASE}/artist/{mbid}?inc=genres+tags&fmt=json")
    if not lookup:
        return []
    return _genres_or_tags(lookup)

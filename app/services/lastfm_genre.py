"""Last.fm kültürel etiket (tag) çekimi — Deezer'ın boş bıraktığı (özellikle TR)
track'ler için fallback.

Akış (canlı test 2026-06-30 ile doğrulandı, TR sanatçılarda 5/5):
  1. track.getTopTags (artist+track) → toptags.tag[].name
  2. boşsa artist.getTopTags (artist) → toptags.tag[].name

API key gerekli (LAST_FM_API_KEY, worker ortam değişkeni). Tag'ler ham/gürültülü
('All', 'baba', sanatçı adı) → normalize_genres ile kanonik türe çevrilir.
"""
from __future__ import annotations

import logging
import urllib.parse
from typing import Any

import httpx

from app.services.genre_errors import RateLimitError

logger = logging.getLogger("rosso.worker.lastfm_genre")

_BASE = "https://ws.audioscrobbler.com/2.0/"
_TAG_LIMIT = 8  # ilk N tag yeterli (normalize zaten eler)


def _top_tags(payload: Any) -> list[str]:
    tags = (payload or {}).get("toptags", {}).get("tag", [])
    return [t["name"] for t in tags if isinstance(t, dict) and t.get("name")]


def _top_tags_scored(payload: Any) -> list[tuple[str, int]]:
    """Tag'leri (name, count) çifti olarak döner (genre DNA skor sistemi için)."""
    tags = (payload or {}).get("toptags", {}).get("tag", [])
    return [
        (t["name"], int(t.get("count") or 0))
        for t in tags
        if isinstance(t, dict) and t.get("name")
    ]


def get_lastfm_tags(artist: str, title: str, api_key: str, http_client: Any) -> list[str]:
    """artist + title → Last.fm top tag listesi (ham). track→artist fallback.

    Bulunamazsa [] döner.
    """
    if not artist or not api_key:
        return []

    a = urllib.parse.quote(artist)
    t = urllib.parse.quote(title or "")

    # 1. track.getTopTags
    try:
        url = (f"{_BASE}?method=track.gettoptags&artist={a}&track={t}"
               f"&api_key={api_key}&format=json")
        resp = http_client.get(url, timeout=10)
        resp.raise_for_status()
        tags = _top_tags(resp.json())
        if tags:
            return tags[:_TAG_LIMIT]
    except Exception:  # noqa: BLE001
        logger.warning("Last.fm track.getTopTags başarısız: %s - %s", artist, title)

    # 2. artist.getTopTags fallback
    try:
        url = f"{_BASE}?method=artist.gettoptags&artist={a}&api_key={api_key}&format=json"
        resp = http_client.get(url, timeout=10)
        resp.raise_for_status()
        return _top_tags(resp.json())[:_TAG_LIMIT]
    except Exception:  # noqa: BLE001
        logger.warning("Last.fm artist.getTopTags başarısız: %s", artist)
        return []


def get_lastfm_track_tags_scored(
    artist: str, title: str, api_key: str, http_client: Any
) -> list[tuple[str, int]]:
    """track.getTopTags → (name, count) çiftleri. Boşsa []. (genre DNA skor sistemi)"""
    if not artist or not api_key:
        return []
    a = urllib.parse.quote(artist)
    t = urllib.parse.quote(title or "")
    try:
        url = (f"{_BASE}?method=track.gettoptags&artist={a}&track={t}"
               f"&api_key={api_key}&format=json")
        resp = http_client.get(url, timeout=10)
        resp.raise_for_status()
        return _top_tags_scored(resp.json())[:_TAG_LIMIT]
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code == 429:
            ra = exc.response.headers.get("Retry-After")
            raise RateLimitError("lastfm", float(ra) if ra else None) from exc
        logger.warning("Last.fm track.getTopTags (scored) başarısız: %s - %s", artist, title)
        return []
    except Exception:  # noqa: BLE001
        logger.warning("Last.fm track.getTopTags (scored) başarısız: %s - %s", artist, title)
        return []


def get_lastfm_artist_tags_scored(
    artist: str, api_key: str, http_client: Any
) -> list[tuple[str, int]]:
    """artist.getTopTags → (name, count) çiftleri. Boşsa []. (genre DNA skor sistemi)"""
    if not artist or not api_key:
        return []
    a = urllib.parse.quote(artist)
    try:
        url = f"{_BASE}?method=artist.gettoptags&artist={a}&api_key={api_key}&format=json"
        resp = http_client.get(url, timeout=10)
        resp.raise_for_status()
        return _top_tags_scored(resp.json())[:_TAG_LIMIT]
    except Exception:  # noqa: BLE001
        logger.warning("Last.fm artist.getTopTags (scored) başarısız: %s", artist)
        return []

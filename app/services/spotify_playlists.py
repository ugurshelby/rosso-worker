"""Spotify playlist listeleme + snapshot_id ucuz kontrol + item pagination.

snapshot_id: playlist_refresh cron'unun "değişti mi" kontrolü için ucuz çağrı
(sadece metadata, track listesi çekilmez) — NotebookLM doğrulaması.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger("rosso.worker.spotify_playlists")

_PLAYLISTS_URL = "https://api.spotify.com/v1/me/playlists"

# ── Hız geçidi (CLAUDE.md §1.6) ────────────────────────────────────────────
# 2026-07-20 denetimi: bu dosyadaki iki `while url:` sayfalama döngüsü dış
# API'ye GECİKMESİZ arka arkaya istek atıyordu. 50+ playlist'i olan bir
# kullanıcıda bu, saniyeler içinde 10+ ardışık istek demek — Spotify'ın
# 2026-07-12'de bize 6,4 SAAT ceza verdiği desenin aynısı.
# Çözüm: deezer_genre.py'deki kanıtlanmış `_paced_get` deseni (~4 istek/sn).
_PACE_GAP_S = 0.25
_pace_lock = threading.Lock()
_last_request_at = 0.0


def _paced_get(http: Any, url: str, **kwargs: Any) -> Any:
    """Bu modüldeki TÜM Spotify çağrılarının tek geçidi — küresel 250ms aralık."""
    global _last_request_at
    with _pace_lock:
        wait = _last_request_at + _PACE_GAP_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()
    return http.get(url, **kwargs)


def _largest_image(images: Any) -> str | None:
    """Spotify `images` dizisinden en büyük görselin URL'i.

    Spotify genelde büyükten küçüğe sıralı döner ama bu SÖZLEŞME DEĞİL —
    width alanı bazen None gelir (playlist mozaik kapakları). O yüzden
    sıralamaya güvenmiyor, width'e göre seçiyoruz; width yoksa 0 sayılır,
    yani hepsi None ise ilk görsel kullanılır.
    """
    if not isinstance(images, list) or not images:
        return None
    best = max(
        (im for im in images if isinstance(im, dict) and im.get("url")),
        key=lambda im: im.get("width") or 0,
        default=None,
    )
    return best.get("url") if best else None


def fetch_user_playlists(access_token: str, http: Any) -> list[dict]:
    """Kullanıcının tüm playlist'lerini döner (pagination dahil)."""
    headers = {"Authorization": f"Bearer {access_token}"}
    results: list[dict] = []
    url = _PLAYLISTS_URL
    params: dict[str, Any] | None = {"limit": 50}

    while url:
        resp = _paced_get(http, url, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("items", []):
            # Spotify API (2026 şeması) playlist.tracks yerine playlist.items
            # kullanıyor — canlı doğrulandı, 2026-07-02. Eski şema (tracks)
            # önce denenir (varsa), yoksa yeni şemaya (items) düşülür.
            track_count = (item.get("tracks") or {}).get("total")
            if track_count is None:
                track_count = (item.get("items") or {}).get("total", 0)
            results.append({
                "spotify_id": item.get("id"),
                "name": item.get("name", ""),
                "snapshot_id": item.get("snapshot_id"),
                "track_count": track_count,
                # 🔴 2026-07-21: Bu iki alan AYNI yanıtta zaten geliyordu ama
                # okunmadan atılıyordu — 105/105 playlist'te cover_url ve
                # description NULL'dı. Üç ekran (playlist listesi, playlist
                # detayı, profilde sabitlenen playlist) bunları okuyor.
                # Ek istek maliyeti SIFIR: veri elimizde, sadece almıyorduk.
                "cover_url": _largest_image(item.get("images")),
                # Spotify boş açıklamayı "" olarak döner; NULL'a çeviriyoruz ki
                # "açıklama yok" ile "boş açıklama" ayrımı DB'de net olsun.
                "description": (item.get("description") or "").strip() or None,
                # B6: sahiplik filtresi için. Kullanıcının TAKİP ettiği (başkasının)
                # playlist'leri item fetch'te 403 verir ve cron'u kırar; ayrıca
                # onlar kullanıcının "kendi" listesi değil. owner.id ile ayıklanır.
                "owner_id": (item.get("owner") or {}).get("id"),
            })
        url = data.get("next")
        params = None

    return results


def fetch_playlist_snapshot_id(access_token: str, playlist_id: str, http: Any) -> str | None:
    """Ucuz metadata çağrısı — sadece snapshot_id, track listesi çekilmez."""
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = _paced_get(
        http,
        f"https://api.spotify.com/v1/playlists/{playlist_id}",
        headers=headers,
        params={"fields": "snapshot_id"},
    )
    resp.raise_for_status()
    return resp.json().get("snapshot_id")


def fetch_playlist_items(access_token: str, playlist_id: str, http: Any) -> list[dict]:
    """Playlist'in tüm track'lerini pozisyon sırasıyla döner (pagination dahil)."""
    headers = {"Authorization": f"Bearer {access_token}"}
    results: list[dict] = []
    url = f"https://api.spotify.com/v1/playlists/{playlist_id}/items"
    params: dict[str, Any] | None = {"limit": 100}
    position = 0

    while url:
        resp = _paced_get(http, url, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("items", []):
            # Spotify 2026 şeması: track objesi artık item["track"] değil
            # item["item"] altında (canlı doğrulandı, 2026-07-03). Eski anahtar
            # geriye dönük uyumluluk için fallback tutulur.
            track = item.get("item") or item.get("track") or {}
            # is_local track'ler ve id'siz item'lar (çöp kaynağı) atlanır.
            if item.get("is_local") or not track.get("id"):
                continue
            artists = [a.get("name", "") for a in track.get("artists", [])]
            results.append({
                "spotify_id": track.get("id"),
                "isrc": (track.get("external_ids") or {}).get("isrc"),
                "title": track.get("name", ""),
                "artists": artists,
                "position": position,
                "added_at": item.get("added_at"),
            })
            position += 1
        url = data.get("next")
        params = None

    return results

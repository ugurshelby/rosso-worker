"""Spotify /me/player/recently-played — pagination ile sıfır veri kaybı fetch.

NotebookLM doğrulaması (2026-07-02): limit=50 her çağrıda mutlak tavan,
ama yanıttaki `next` URL'i takip edilirse bir saatte 50'den fazla dinlenen
track'ler de eksiksiz çekilir. after parametresi sadece İLK çağrıda kullanılır;
next URL'i zaten kendi parametrelerini taşır.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger("rosso.worker.spotify_recently_played")

_BASE_URL = "https://api.spotify.com/v1/me/player/recently-played"

# ── Hız geçidi (CLAUDE.md §1.6, 2026-07-20 denetimi) ───────────────────────
# Bu modül 429'u DOĞRU yakalıyordu (SpotifyRateLimitError + Retry-After) ama
# ÖNLEYİCİ gecikmesi yoktu: `while url:` sayfalaması cezayı yedikten sonra
# tepki veriyor, yemeden önce engellemiyordu. §1.6'nın ilk kuralı "istekler
# arası ≥250ms" — tepki değil, önlem.
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


class SpotifyAuthError(Exception):
    """401 — token geçersiz/revoke edilmiş."""


class SpotifyForbiddenError(Exception):
    """403 — token sağlam ama kullanıcı bu uygulamaya (Dev Mode allowlist) kayıtlı değil.

    401'den ayrılır: token'ı revoke etme (is_active=false yapma) YANLIŞ olur —
    sorun izin, kimlik değil. Runner bunu ayrı işaretler, kullanıcıya dürüst mesaj gösterir.
    """


class SpotifyRateLimitError(Exception):
    """429 — rate limit. retry_after saniye cinsinden (Retry-After header, yoksa None)."""

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("spotify rate limited")
        self.retry_after = retry_after


def _largest_image(images: Any) -> str | None:
    """Spotify images dizisinden en büyük görsel URL'i (width'e göre, sıra değil).

    Sıraya güvenilmez: Spotify çoğunlukla büyükten küçüğe döndürür ama bunu
    garanti etmez. `cover_backfill_runner._largest_image` ile aynı davranış.
    """
    if not isinstance(images, list) or not images:
        return None
    best = max(
        (im for im in images if isinstance(im, dict) and im.get("url")),
        key=lambda im: im.get("width") or 0,
        default=None,
    )
    return best.get("url") if best else None


def fetch_recently_played(
    access_token: str, after_ms: int | None, http: Any
) -> list[dict]:
    """Son played_at'ten (after_ms) bu yana TÜM yeni track'leri döner (pagination dahil)."""
    headers = {"Authorization": f"Bearer {access_token}"}
    results: list[dict] = []

    url = _BASE_URL
    params: dict[str, Any] | None = {"limit": 50}
    if after_ms is not None:
        params["after"] = after_ms

    while url:
        try:
            resp = _paced_get(http, url, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 401:
                raise SpotifyAuthError() from exc
            if status == 403:
                # Token sağlam ama kullanıcı allowlist'te değil — sessizce yutma.
                # Boş liste dönmek "yeni şarkı yok"la karışır ve cron "success" der.
                raise SpotifyForbiddenError() from exc
            if status == 429:
                ra_header = getattr(exc.response, "headers", {}).get("Retry-After")
                raise SpotifyRateLimitError(float(ra_header) if ra_header else None) from exc
            logger.warning("Spotify recently-played fetch başarısız: status=%s", status)
            break

        for item in data.get("items", []):
            track = item.get("track") or {}
            artists = [a.get("name", "") for a in track.get("artists", [])]
            results.append({
                "played_at": item.get("played_at"),
                "spotify_id": track.get("id"),
                "title": track.get("name", ""),
                "artists": artists,
                "duration_ms": track.get("duration_ms", 0),
                # Albüm kapağı — plan 08 (2026-08-05).
                #
                # ⚠ Bu alan yanıtta ZATEN GELİYOR, kod onu atıyordu; sonra kör
                # dolgu cron'u aynı görseli AYRI bir istekle tekrar istiyordu.
                # Okumak ek istek DEĞİL, mevcut israfı kaldırır.
                #
                # Dashboard'un ilk gördüğü bölüm "Son Dinlenenler" — kapağın
                # burada yazılması, o ekranın DB'den hazır gelmesini sağlar.
                "image_url": _largest_image((track.get("album") or {}).get("images")),
                # B8: /me/player/recently-played ISRC VERMİYOR — `external_ids` bu uçta
                # hiç gelmiyor (canlı doğrulama 2026-07-02 ve 2026-07-11: 5/5 şarkıda
                # None). Yani bu satır pratikte hep None üretir ve aşağı akıştaki ISRC
                # yazma dalı hiç çalışmaz.
                #
                # Yine de KALDIRMIYORUZ: alan gelirse bedavaya doğru çalışır (yazma dalı
                # zaten `.is_("isrc","null")` korumalı, mevcut ISRC'yi ezmez). Kaldırmak
                # Spotify alanı geri getirdiğinde sessiz bir veri kaybı olurdu.
                # ISRC'nin gerçek kaynağı: ZIP export + genre_runner zenginleştirmesi.
                "isrc": (track.get("external_ids") or {}).get("isrc"),
            })

        url = data.get("next")
        params = None  # next URL zaten tüm parametreleri içerir

    return results

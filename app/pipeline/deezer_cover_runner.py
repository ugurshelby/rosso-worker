"""Deezer kapak dolgusu — Spotify'ı OLMAYAN kullanıcıların kapakları (2026-09-24).

Efendim §14 (docs/plans/yeni-kullanici-deneyimi-quick-start.md): yalnız ZIP yükleyen
kullanıcının Spotify bağlantısı yoktur; onun paketlerindeki şarkı/sanatçı kapakları
Deezer'dan doldurulur. Web'in canlı yolu (`/api/images/track/[id]`) kullanıcı bakınca
anında çeker; bu runner ARKA PLAN tamamlayıcısıdır (cron): hiç bakılmamış, paketlerde
duran eksikleri doldurur ki bir sonraki bakışta kapak DB'den hazır gelsin.

Yazım: `deezer_image_url` (+ `deezer_kapak_denendi_at`). `image_url` ve öncelik kuralı
(Spotify her zaman kazanır) DB tetikleyicisinde (migration 0349) — burada çakışma
yönetimi YOK; hangi sırayla yazılırsa yazılsın sonuç aynı.

Doğrulama: track için `isrc_backfill.lookup_track_payload` (fold'lu arama + `_pick_best`,
canlı kalibre eşikler: yanlış kabul 0/80) — yaklaşık eşleşme KABUL EDİLMEZ, çünkü
YANLIŞ KAPAK KAPAKSIZDAN KÖTÜDÜR. Sanatçı için ada TAM (fold'lu) eşleşme.

Hız: tüm Deezer çağrıları `deezer_genre._paced_get` (küresel ≥250 ms; limit 50/5 sn).
Kota (429 / gövdede code 4): tur biter, `deezer` cooldown'ı yazılır; satırlar
"denendi" DAMGALANMAZ (geçici, sonra yeniden denenir). Kesin "yok" 30 gün damgalanır.
"""
from __future__ import annotations

import logging
import re
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any

from app.pipeline.isrc_backfill import lookup_track_payload
from app.services import cooldown
from app.services.deezer_genre import _paced_get, _raise_if_quota, fold
from app.services.genre_errors import RateLimitError

logger = logging.getLogger("rosso.worker.deezer_cover_runner")

_PROVIDER = "deezer"
_TRACK_ISRC_URL = "https://api.deezer.com/track/isrc:{isrc}"
_ARTIST_SEARCH_URL = "https://api.deezer.com/search/artist"

#: Boş md5 = Deezer'ın "kapak yok" yer tutucusu → reddedilir.
_KAPAK_RE = re.compile(
    r"^https://(?:cdn-images|e-cdns-images)\.dzcdn\.net/images/(?:cover|artist)/[0-9a-f]{32}/",
    re.IGNORECASE,
)
_ISRC_RE = re.compile(r"^[A-Za-z0-9]{12}$")
_DEFAULT_COOLDOWN_S = 600.0


class _ButceDoldu(Exception):
    """Zaman bütçesi bitti — tur 'partial' ile kapanır."""


def gecerli_kapak(url: Any) -> str | None:
    """Geçerli Deezer CDN kapağı ise URL, değilse None (yer tutucu/yabancı alan adı)."""
    return url if isinstance(url, str) and _KAPAK_RE.match(url) else None


def _payload_kapagi(payload: dict[str, Any] | None) -> str | None:
    album = (payload or {}).get("album") or {}
    return gecerli_kapak(album.get("cover_big")) or gecerli_kapak(album.get("cover_xl"))


def _get_json(http: Any, url: str) -> Any:
    """Tek Deezer GET'i: 429/gövde-kotası → RateLimitError; diğer hata → None."""
    import httpx

    try:
        resp = _paced_get(http, url)
        resp.raise_for_status()
        payload = resp.json()
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code == 429:
            ra = exc.response.headers.get("Retry-After")
            raise RateLimitError(_PROVIDER, float(ra) if ra else None) from exc
        return None
    except Exception:  # noqa: BLE001
        return None
    _raise_if_quota(payload)
    return payload


def track_kapagi(row: dict[str, Any], http: Any) -> str | None:
    """Bir şarkının Deezer kapağı: önce ISRC (kesin), sonra doğrulanmış arama."""
    isrc = (row.get("isrc") or "").strip()
    if _ISRC_RE.match(isrc):
        url = _payload_kapagi(_get_json(http, _TRACK_ISRC_URL.format(isrc=isrc)))
        if url:
            return url

    artists = row.get("artists") or []
    artist = artists[0] if artists else ""
    return _payload_kapagi(lookup_track_payload(artist, row.get("title") or "", http))


def sanatci_gorseli(ad: str, http: Any) -> str | None:
    """Ada TAM (fold'lu) eşleşen sanatçının Deezer görseli. Yaklaşık eşleşme yok."""
    if not ad.strip():
        return None
    q = urllib.parse.quote(f'"{ad}"')
    payload = _get_json(http, f"{_ARTIST_SEARCH_URL}?q={q}&limit=8")
    hedef = fold(ad)
    for a in (payload or {}).get("data") or []:
        if fold(a.get("name")) != hedef:
            continue
        url = gecerli_kapak(a.get("picture_big")) or gecerli_kapak(a.get("picture_xl"))
        if url:
            return url
    return None


def _simdi() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_deezer_cover_backfill(
    client: Any,
    http: Any,
    *,
    batch_limit: int = 80,
    time_budget_s: float = 150.0,
) -> dict[str, Any]:
    """Paketlerde duran, kapağı olmayan şarkı ve sanatçıları Deezer'dan doldurur.

    {outcome, processed, updated, skipped, quota_hit}. outcome: 'empty' | 'success' |
    'partial' (kota/bütçe) | 'blocked' (cooldown aktif).
    """
    baslangic = time.monotonic()
    bloklu, kalan = cooldown.is_blocked(client, _PROVIDER)
    if bloklu:
        logger.info("Deezer kapak dolgusu: cooldown aktif (%ds) — tur atlandı", kalan)
        return {"outcome": "blocked", "processed": 0, "updated": 0, "skipped": 0, "quota_hit": False}

    sarkilar = (client.rpc("deezer_kapak_adaylari_track", {"p_limit": batch_limit}).execute().data) or []
    sanatcilar = (client.rpc("deezer_kapak_adaylari_sanatci", {"p_limit": batch_limit}).execute().data) or []
    if not sarkilar and not sanatcilar:
        return {"outcome": "empty", "processed": 0, "updated": 0, "skipped": 0, "quota_hit": False}

    islenen = guncellenen = atlanan = 0
    yarida_kaldi = False

    def yaz(tablo: str, satir_id: str, url: str | None) -> bool:
        """Bulunduysa görsel + damga; kesin yoksa YALNIZ damga (30 gün tekrar aranmaz)."""
        veri: dict[str, Any] = {"deezer_kapak_denendi_at": _simdi()}
        if url:
            veri["deezer_image_url"] = url
        try:
            client.table(tablo).update(veri).eq("id", satir_id).execute()
            return url is not None
        except Exception:  # noqa: BLE001
            logger.warning("Deezer kapak yazımı başarısız: %s %s", tablo, satir_id)
            return False

    try:
        for tablo, satirlar, cozucu in (
            ("tracks", sarkilar, lambda r: track_kapagi(r, http)),
            ("artists", sanatcilar, lambda r: sanatci_gorseli(r.get("name") or "", http)),
        ):
            for satir in satirlar:
                if time.monotonic() - baslangic > time_budget_s:
                    raise _ButceDoldu
                islenen += 1
                url = cozucu(satir)
                if yaz(tablo, satir["id"], url):
                    guncellenen += 1
                else:
                    atlanan += 1
    except _ButceDoldu:
        yarida_kaldi = True
    except RateLimitError as exc:
        # Geçici: damga VURULMAZ; cooldown Deezer'ın istediği süreyle.
        cooldown.set_cooldown(
            client, _PROVIDER, exc.retry_after or _DEFAULT_COOLDOWN_S, reason="deezer_cover_429"
        )
        logger.warning("Deezer kapak dolgusu: kota — tur durduruldu")
        yarida_kaldi = True

    outcome = "partial" if yarida_kaldi else ("success" if guncellenen else "empty")
    return {
        "outcome": outcome,
        "processed": islenen,
        "updated": guncellenen,
        "skipped": atlanan,
        "quota_hit": yarida_kaldi,
    }

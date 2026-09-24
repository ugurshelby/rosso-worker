"""Katalog dolgusu — ISRC + duration_ms + album + release_year.

── NEDEN (NotebookLM raporu + canlı ölçüm, 2026-07-12) ──

1) **ISRC = %2,5** (292/11.691). Taşıma motoru (src/lib/migration/engine.ts) ÖNCE
   ISRC arıyor (`search?q=isrc:...`); bulamazsa isim+sanatçı **bulanık aramasına**
   düşüyor. Yani pratikte TÜM taşıma en zayıf yolla yapılıyor — "Nowadays" diye
   arayıp ilk çıkanı alıyor.

   Ama `spotify_id` = **%99,8** (11.668/11.691). ⚠ Raporun "batch endpoint
   çalışıyor, 234 istek yeter" iddiası CANLIDA YANLIŞ ÇIKTI (403) — doğrusu
   aşağıda `fetch_batch`'te: tekil uç, 50 kat istek, 250ms hız geçidi.

   🔵 2026-07-20: ISRC dolgusunun ASIL yolu artık `isrc_backfill.py` (Deezer
   öncelikli, cezasız). Bu Spotify yolu yalnız kalıntı/doğrulama katmanı olarak
   duruyor; cron'u aylık ve fiilen kapalı.

2) **duration_ms = 11.692/11.692 NULL.**
   ⚠ Rapor "duration_ms = 0 → çalınamaz içerik" dedi — **YANILDI**. Değer 0 değil,
   NULL. Veri hiç yazılmamış. Kök neden bulundu: `insert_tracks_batch` RPC'si bu
   alanı yazmaya HAZIR, ama `export_runner.py` payload'a KOYMUYOR. Zaten ZIP
   export'u da vermiyor (yalnız `ms_played` var = dinlenen süre, `duration_ms`
   = şarkının tam süresi yok).

Dördü de aynı endpoint'ten gelir → **tek işte dört sorun**.

── Güvenlik ağları ──
- Batch = 50 (Spotify sınırı). Rapor: tekil istek Development Mode'da 403 riski.
- Bulunamayan track de `catalog_backfill_at` alır → **sonsuz retry yok**.
- 429/kota → `SpotifyQuotaExhausted` (mevcut altyapı) → cron sessizce çıkar,
  bir sonraki tur devam eder. Dolgu **kaldığı yerden** sürer.
- COALESCE ile yazılır (RPC) → dolgu asla veri SİLMEZ.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from app.services import cooldown
from app.services.spotify_kimlik_havuzu import KimlikGrubu
from app.services.spotify_lookup import (
    SpotifyQuotaExhausted,
    _get_access_token,
    _with_retry,
)

logger = logging.getLogger("rosso.worker.catalog_backfill")

# Tur başına kaç track. Toplu uç 403 verdiği için her track AYRI istek demek
# (bkz. fetch_batch) → 50 istek/tur.
_BATCH = 50

# 🔴 CANLI BEDEL (2026-07-12): tekil uca geçince cron 50 isteği ARKA ARKAYA attı.
# Spotify uygulamayı cezalandırdı: **Retry-After = 22.881 sn (6,4 SAAT)** — tek bir
# yavaş istek bile 429 alır oldu. Toplu uç 1 istek atıyordu, tekil uç 50 atıyor;
# 50 kat trafik. 403'ü çözerken daha büyük bir sorun yarattım.
#
# İki koruma:
#   1. İstekler arası gecikme (aşağıda) — Spotify'ı dürtmemek için.
#   2. DEVRE KESİCİ: 429'da DB'ye cooldown yaz. ⚠ Bu cron cooldown'ı HİÇ
#      KULLANMIYORDU — altyapı vardı (services/cooldown.py), ama bu dosya
#      ondan habersizdi. Bu yüzden her tur körü körüne yeniden deniyor ve
#      cezayı uzatıyordu.
_REQUEST_DELAY_S = 0.25   # ~4 istek/sn — Spotify'ın rahat tolere ettiği hız
_PROVIDER = "spotify"


def _retry_after_seconds(exc: Exception) -> float:
    """SpotifyQuotaExhausted'tan Retry-After saniyesini okur.

    2026-08-01'den beri süre exception'ın ALANI (`exc.retry_after`); mesaj
    metnini regex'le ayıklamak gerekmez. Eski davranış yedek olarak duruyor —
    başka bir yol bu exception'ı süre vermeden fırlatırsa yine de çalışsın.

    Süre okunamazsa (HTTP/2 kopması gibi) **1 saat** varsayılır: sıfır varsaymak
    devre kesiciyi işlevsiz bırakır (cron hemen yine dener ve cezayı besler).
    Fazla beklemek, az beklemekten iyidir — Spotify'ı kızdırmak pahalıya patlıyor.
    """
    retry_after = getattr(exc, "retry_after", None)
    if retry_after:
        return float(retry_after)

    import re

    m = re.search(r"Retry-After=([\d.]+)", str(exc))
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return 3600.0


def _release_year(album: dict[str, Any] | None) -> int | None:
    """album.release_date → yıl. Format 'YYYY' | 'YYYY-MM' | 'YYYY-MM-DD'."""
    if not album:
        return None
    rd = album.get("release_date")
    if not rd or not isinstance(rd, str):
        return None
    head = rd[:4]
    if not head.isdigit():
        return None
    year = int(head)
    # Saçma yılları yazma (bozuk veri) — 1900 öncesi kayıtlı müzik yok.
    return year if 1900 <= year <= 2100 else None


def parse_track(t: dict[str, Any] | None) -> dict[str, Any] | None:
    """Spotify track nesnesi → dolgu satırı. Geçersizse None."""
    if not t or not t.get("id"):
        return None
    album = t.get("album") or {}
    return {
        "spotify_id": t["id"],
        "isrc": (t.get("external_ids") or {}).get("isrc"),
        "duration_ms": t.get("duration_ms"),
        "album": album.get("name"),
        "release_year": _release_year(album),
    }


def fetch_batch(ids: list[str], token: str, http: Any) -> list[dict[str, Any]]:
    """Track'leri TEKİL uçtan çeker (`GET /v1/tracks/{id}`).

    🔴 CANLI BULGU (2026-07-12): toplu uç (`GET /v1/tracks?ids=`) Development
    Mode'da **403 Forbidden** veriyor. Cron ilk turda patladı.

    Kanıt (canlı ölçüm, aynı 3 id):
        /v1/tracks?ids=a,b,c   → HTTP 403 Forbidden
        /v1/tracks/{id} × 3    → HTTP 200, 3/3 (ISRC + duration geldi)

    ⚠ NotebookLM raporu "batch endpoint 50'lik çalışıyor, 234 istek" dedi —
    **YANILDI**. Bu tuzak zaten biliniyordu (hafıza: "Spotify Dev Mode batch
    /tracks 403"), ama rapora güvenilip atlandı. Rapor da olsa **ölçmeden inanma**.

    Maliyet: 50 kat fazla istek (234 → ~11.700). Rate-limit'e takılmamak için
    `_with_retry` her çağrıda uygulanır; kota tükenirse SpotifyQuotaExhausted
    yukarı fırlar ve cron 'partial' der (veri kaybı yok, sonraki tur devam eder).

    Bulunamayan track (silinmiş/bölgesel) yine de işaretlenir → sonsuz retry olmaz.
    """
    out: list[dict[str, Any]] = []

    for i, sid in enumerate(ids):
        # Hız kısıtlama: istekleri ARKA ARKAYA atmak Spotify'a 6,4 saatlik ceza
        # yazdırdı (canlı olay). İlk istekten önce beklemeye gerek yok.
        if i > 0:
            time.sleep(_REQUEST_DELAY_S)

        def _do(track_id: str = sid) -> Any:
            r = http.get(
                f"https://api.spotify.com/v1/tracks/{track_id}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=20.0,
            )
            # 404 = silinmiş/bölgesel. Hata DEĞİL — işaretlenip geçilir.
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r

        resp = _with_retry(_do)
        row = parse_track(resp.json()) if resp is not None else None

        if row:
            out.append(row)
        else:
            # Bulunamadı: yine de catalog_backfill_at alsın, yoksa kuyruk
            # hiç boşalmaz ve her turda aynı id tekrar denenir.
            out.append({
                "spotify_id": sid,
                "isrc": None, "duration_ms": None, "album": None, "release_year": None,
            })

    return out


def run_one_catalog_batch(
    client: Any, settings: Any, *, max_batches: int = 8, grup: KimlikGrubu | None = None
) -> dict[str, Any]:
    """Bir cron turunda `max_batches` × 50 track dolgula.

    max_batches=8 → tur başına 400 track, 8 istek. 11.668 track ≈ 30 tur.
    Cron 5 dakikada bir çalışıyorsa katalog ~2,5 saatte dolar.
    """
    import httpx

    processed = 0
    updated = 0

    # 🔴 DEVRE KESİCİ — bu cron cooldown'ı HİÇ KULLANMIYORDU (canlı bulgu).
    # Spotify 6,4 saatlik ceza verdiğinde her 10 dakikada bir yeniden deniyor,
    # cezayı besliyordu. Bloklu isek HİÇ İSTEK ATMA.
    saglayici = grup.saglayici if grup else _PROVIDER
    blocked, remaining = cooldown.is_blocked(client, saglayici)
    if blocked:
        logger.warning(
            "Spotify cooldown aktif (%d sn / %.1f saat) — dolgu atlanıyor",
            remaining, remaining / 3600,
        )
        return {
            "outcome": "blocked",
            "processed": 0,
            "updated": 0,
            "cooldown_remaining_s": remaining,
        }

    with httpx.Client(timeout=httpx.Timeout(20.0)) as http:
        # App token (client credentials) — kullanıcı oturumu GEREKMEZ, katalog
        # verisi herkese aynı. Kullanıcı token'ı kullanmak yanlış olurdu:
        # kullanıcı bağlantısını kesince dolgu ölürdü.
        token = _get_access_token(
            grup.client_id if grup else settings.spotify_client_id,
            grup.client_secret if grup else settings.spotify_client_secret,
            http,
        )
        if not token:
            logger.warning("Spotify app token alınamadı — dolgu atlanıyor")
            return {"outcome": "error", "processed": 0, "updated": 0, "error": "no_token"}

        for _ in range(max_batches):
            if grup:
                # Yalnız bu grubun kullanıcılarının dinlediği bekleyen track'ler.
                res = client.rpc(
                    "katalog_dolgu_adaylari_kullanicilar",
                    {"p_user_ids": grup.user_ids, "p_limit": _BATCH},
                ).execute()
            else:
                res = (
                    client.table("tracks")
                    .select("spotify_id")
                    .is_("catalog_backfill_at", "null")
                    .not_.is_("spotify_id", "null")
                    .or_("isrc.is.null,duration_ms.is.null")
                    .order("created_at")
                    .limit(_BATCH)
                    .execute()
                )
            ids = [r["spotify_id"] for r in (res.data or []) if r.get("spotify_id")]
            if not ids:
                # Kuyruk boş — iş bitti.
                return {
                    "outcome": "empty" if processed == 0 else "success",
                    "processed": processed,
                    "updated": updated,
                }

            try:
                rows = fetch_batch(ids, token, http)
            except SpotifyQuotaExhausted as exc:
                # Kota tükendi: o ana kadar yazılanlar KORUNUR, tur biter.
                #
                # 🔴 EKSİK OLAN BUYDU: cooldown DB'ye YAZILMIYORDU. Sonuç: her 10
                # dakikada bir cron yine deniyor, yine 429 yiyor, cezayı besliyordu.
                # Artık devre kesici kurulur → sonraki turlar HİÇ istek atmaz.
                retry_after = _retry_after_seconds(exc)
                used = cooldown.set_cooldown(
                    client, saglayici, retry_after, reason="catalog_429"
                )
                logger.warning(
                    "Spotify kotası tükendi — %d sn (%.1f saat) cooldown yazıldı: %s",
                    used, used / 3600, exc,
                )
                return {
                    "outcome": "partial",
                    "processed": processed,
                    "updated": updated,
                    "error": "quota_exhausted",
                    "cooldown_s": used,
                }

            if not rows:
                logger.warning("Batch boş döndü — dolgu duraklatıldı")
                return {
                    "outcome": "partial", "processed": processed, "updated": updated,
                    "error": "empty_batch",
                }

            try:
                w = client.rpc("apply_catalog_backfill", {"p_rows": rows}).execute()
                updated += int(w.data or 0)
            except Exception as exc:  # noqa: BLE001
                # Yazım hatası SESSİZCE yutulmaz (B19 dersi) — tur error ile biter.
                logger.exception("apply_catalog_backfill yazım hatası")
                return {
                    "outcome": "error", "processed": processed, "updated": updated,
                    "error": str(exc)[:300],
                }

            processed += len(ids)

    return {"outcome": "success", "processed": processed, "updated": updated}

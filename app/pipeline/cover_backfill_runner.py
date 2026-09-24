"""Kapak dolgusu — dinlenen track'lerin image_url'ini Spotify'dan doldurur.

FAZ DİNLEME-GEÇMİŞİ Katman 0 (Efendim 2026-07-26): /gecmis sayfası kapaklı
grid gösteriyor ama tracks.image_url yalnız %5,9 dolu. Bu runner boş kapakları
Spotify'dan doldurur.

⚠ §1.6 (RATE-LIMIT — 6,4 saat ceza dersi): Dev Mode'da toplu uç /v1/tracks?ids=
403 verir → tekil /v1/tracks/{id} kullanılır → 50 kat istek. Bu yüzden:
  - İstekler arası ≥250ms geçit (_paced_get, ~4 istek/sn).
  - batch_limit + time_budget_s ile SINIRLI — bir turda az track, cron devralır.
  - 429'da HEMEN dur (SpotifyQuotaExhausted) — turu bitir, sonraki tur bekler.
  - image_url'e yazar (album_image_url DEĞİL — o sütun DB'de yok, §1.5 ölçümü).

§1.7: v1 = yalnız TRACK kapağı (spotify_id'si olanlar). Sanatçı görseli
(spotify_artist_ids %0 dolu → ekstra search katmanı gerekir) v2'ye bırakıldı.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from app.services import cooldown
from app.services.spotify_kimlik_havuzu import KimlikGrubu
from app.services.spotify_lookup import (
    _get_access_token,
    _with_retry,
    SpotifyQuotaExhausted,
)

logger = logging.getLogger("rosso.worker.cover_backfill_runner")

# Ortak Spotify Client Credentials havuzu bu anahtarla takip edilir. Genre artist
# fallback + katalog da AYNI havuzu paylaşır — kapak dolgusu 429 yerse bu damgayı
# vurur, diğer Spotify işleri is_blocked ile onu görüp durur (ceza beslenmez).
_SPOTIFY_PROVIDER = "spotify"

#: Kat-1 aday kuyruğu (migration 0221). Kör kuyruk `cover_backfill_candidates`
#: hâlâ duruyor ama VARSAYILAN DEĞİL — bkz. `run_cover_backfill` docstring'i.
_KAT1_RPC = "paket_gorsel_adaylari_track"

# §1.6: küresel 250ms geçit (playlist_refresh'teki kanıtlanmış desen).
_PACE_GAP_S = 0.25
_pace_lock = threading.Lock()
_last_request_at = 0.0


def _paced_get(http: Any, url: str, **kwargs: Any) -> Any:
    global _last_request_at
    with _pace_lock:
        wait = _last_request_at + _PACE_GAP_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()
    return http.get(url, **kwargs)


def _largest_image(images: Any) -> str | None:
    """Spotify images dizisinden en büyük görsel URL'i (width'e göre, sıra değil)."""
    if not isinstance(images, list) or not images:
        return None
    best = max(
        (im for im in images if isinstance(im, dict) and im.get("url")),
        key=lambda im: im.get("width") or 0,
        default=None,
    )
    return best.get("url") if best else None


def run_cover_backfill(
    client: Any,
    settings: Any,
    http: Any,
    batch_limit: int = 100,
    time_budget_s: float = 200.0,
    candidates_rpc: str = _KAT1_RPC,
    grup: KimlikGrubu | None = None,
) -> dict[str, Any]:
    """image_url'i boş, spotify_id'si dolu track'leri Spotify'dan doldurur.

    grup (2026-09-23): verilirse tur O GRUBUN app'iyle ve kotasıyla çalışır —
    yalnız grubun kullanıcılarının paketlerindeki eksikler, cooldown
    sağlayıcısı `grup.saglayici`. Verilmezse eski davranış (paylaşılan env).

    {outcome, processed, updated, skipped, quota_hit}. outcome:
      'empty'   — doldurulacak track yok (kuyruk boş, bakım modu).
      'success' — en az bir kapak dolduruldu.
      'partial' — kota/bütçe ile yarıda kaldı (sonraki tur devralır).

    Args:
        candidates_rpc: Aday kuyruğunu veren RPC. Varsayılan **Kat-1**
            (`paket_gorsel_adaylari_track`) — yalnız paketlerde duran eksikler.
            Kör kuyruk (`cover_backfill_candidates`) elle tek seferlik
            çalıştırma için hâlâ geçilebilir; ikisi de aynı `{id, spotify_id}`
            şeklini döner.
    """
    start = time.monotonic()

    # §1.6 ilk kapı: ortak Spotify havuzu bloklu ise HİÇ dokunma — token bile
    # alma. Bloklu olmak, başka bir Spotify işinin (veya önceki kapak turunun)
    # 429 yiyip cooldown damgası vurduğu anlamına gelir. Kapak dolgusu düşük
    # öncelikli — o pencerede diğerlerine yol açar, kotayı yemez.
    saglayici = grup.saglayici if grup else _SPOTIFY_PROVIDER
    blocked, remaining = cooldown.is_blocked(client, saglayici)
    if blocked:
        logger.info(
            "Kapak dolgusu: Spotify cooldown aktif (%ds kaldı) — tur atlandı, kota korunuyor",
            remaining,
        )
        return {"outcome": "blocked", "processed": 0, "updated": 0, "skipped": 0}

    # ── Kat-1 kuyruğu (plan 08 §4, migration 0221) ────────────────────────
    # Varsayılan artık KÖR kuyruk değil: yalnız paketlerin İÇİNDE duran ve
    # görseli eksik olan track'ler. Ölçüldü (2026-08-05):
    #
    #   kör kuyruk   : 25.948 track  → günlerce dolaşır, ceza yer, bitmez
    #   Kat-1 kuyruğu:     49 track  → tek turda biter (~13 sn)
    #
    # Efendim'in kuralı: "Rosso'da göremeyeceği görseli çekme." Kör kuyruk
    # tam da onu yapıyordu; kullanıcının hiç açmayacağı içerik için kota
    # harcayıp paketlerdeki gerçek eksikleri bekletiyordu.
    if grup:
        res = client.rpc(
            "paket_gorsel_adaylari_track_kullanicilar",
            {"p_user_ids": grup.user_ids, "p_limit": batch_limit},
        ).execute()
    else:
        res = client.rpc(candidates_rpc, {"p_limit": batch_limit}).execute()
    rows = res.data or []
    if not rows:
        return {"outcome": "empty", "processed": 0, "updated": 0, "skipped": 0}

    token = _get_access_token(
        grup.client_id if grup else settings.spotify_client_id,
        grup.client_secret if grup else settings.spotify_client_secret,
        http,
    )
    if not token:
        logger.warning("Kapak dolgusu: Spotify token alınamadı — tur atlandı")
        return {"outcome": "error", "processed": 0, "updated": 0, "skipped": 0}

    headers = {"Authorization": f"Bearer {token}"}
    processed = 0
    updated = 0
    skipped = 0
    quota_hit = False

    for row in rows:
        # Bütçe kontrolü — Railway cron penceresini aşma (§1.6 disiplini).
        if time.monotonic() - start > time_budget_s:
            logger.info("Kapak dolgusu: zaman bütçesi doldu, %d işlendi", processed)
            break

        spotify_id = row["spotify_id"]
        try:
            def _fetch() -> Any:
                r = _paced_get(
                    http,
                    f"https://api.spotify.com/v1/tracks/{spotify_id}",
                    headers=headers,
                    timeout=10,
                )
                r.raise_for_status()
                return r

            resp = _with_retry(_fetch)
        except SpotifyQuotaExhausted as exc:
            # 429/kota — devre kesici. Turu bitir, DB'ye yazılanlar korunur.
            # §1.6: ORTAK cooldown'a damga vur → aynı Spotify havuzunu kullanan
            # diğer işler (genre fallback, katalog, sonraki kapak turu) is_blocked
            # ile bunu görüp durur. Ceza kendi kendine beslenmez.
            #
            # ⚠ Cooldown Spotify'ın İSTEDİĞİ süreyle vurulur (exc.retry_after).
            # Sabit süre uydurmak cezayı besler: 2026-08-01'de burada sabit
            # 3600s yazılıyordu, Spotify ise 64926s (18 saat) istiyordu — cron
            # her saat uyanıp yeni 429 yiyor, Retry-After hiç düşmüyordu.
            # Süre bilinmiyorsa (HTTP/2 kopması) §4.2 gereği 1 saat.
            retry_after = exc.retry_after or 3600.0
            cooldown.set_cooldown(
                client, saglayici, retry_after, reason="cover_backfill_429"
            )
            # Kaç saniye YAZILDIĞINI cooldown.set_cooldown loglar (cap'i o bilir).
            # Burada yalnız platformun ne istediğini söyleriz.
            logger.warning(
                "Kapak dolgusu: Spotify kotası tükendi (Retry-After=%.0fs) — cooldown damgası + tur durduruluyor",
                retry_after,
            )
            quota_hit = True
            break
        except Exception:  # noqa: BLE001
            skipped += 1
            processed += 1
            continue

        processed += 1
        if resp is None:
            skipped += 1
            continue

        data = resp.json()
        image_url = _largest_image((data.get("album") or {}).get("images"))
        if not image_url:
            skipped += 1
            continue

        try:
            client.table("tracks").update({"image_url": image_url}).eq("id", row["id"]).execute()
            updated += 1
        except Exception:  # noqa: BLE001
            logger.warning("Kapak yazımı başarısız: track=%s", row["id"])
            skipped += 1

    if quota_hit or (processed < len(rows)):
        outcome = "partial"
    else:
        outcome = "success" if updated else "empty"

    return {
        "outcome": outcome,
        "processed": processed,
        "updated": updated,
        "skipped": skipped,
        "quota_hit": quota_hit,
    }

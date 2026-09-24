"""Sanatçı görseli ön-doldurma — boş artists.image_url'i Spotify'dan doldurur.

Efendim (2026-07-28): "sanatçı görselleri neden yok? görüntülemeye gerek olmadan
yapamıyor muyuz?" — Evet. /api/images/artist görseli YALNIZ kullanıcı ekranda
görünce çekiyordu ("lazy cache on view") → yalnız görüntülenen sanatçılar dolu
(canlı ölçüm %2,5). Bu runner AYNI köprü mantığını ÖNDEN, cron'la çalıştırır.

KÖPRÜ (API route.ts ile birebir aynı, spotify_artist_ids %0 dolu olduğu için):
  sanatçı adını taşıyan TEK-SANATÇILI bir track'in spotify_id'si
  → /v1/tracks/{id} → yanıttaki artists[] içinden ADA eşleşen artist.id
  → /v1/artists/{id} → images[0].url → artists.image_url'e yaz.

⚠ YANLIŞ GÖRSEL KORUMASI (FAZ İ1 dersi, route.ts satır 83): track yanıtındaki
sanatçılardan adı KESİN eşleşen yoksa GÖRSEL YAZMA. Yanlış görseli kalıcı yazmaktansa
boş bırak — sonraki tur başka bir track'le doğru eşleşmeyi dener. RPC zaten
tek-sanatçılı köprü track seçiyor ama Spotify yanıtı çok-sanatçılı olabilir
(feat. metadata), o yüzden ikinci kapı burada da var.

⚠ §1.6 (RATE-LIMIT — 6,4 saat ceza dersi): sanatçı başına İKİ Spotify isteği
(/tracks + /artists) → cover_backfill'den 2× ağır. Bu yüzden:
  - İstekler arası ≥250ms geçit (_paced_get, cover_backfill ile aynı).
  - Ortak "spotify" cooldown havuzunu paylaşır — is_blocked ilk kapı.
  - batch_limit + time_budget_s ile SINIRLI, turlar hâlinde ilerler.
  - 429'da HEMEN dur + cooldown damgası (ceza kendi kendine beslenmez).
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

logger = logging.getLogger("rosso.worker.artist_image_backfill_runner")

# cover_backfill + genre fallback + katalog ile AYNI Spotify havuzu (§1.6).
_SPOTIFY_PROVIDER = "spotify"

#: Kat-1 aday kuyruğu (migration 0221). Kör kuyruk
#: `artist_image_backfill_candidates` duruyor ama VARSAYILAN DEĞİL.
_KAT1_RPC = "paket_gorsel_adaylari_sanatci"

# §1.6: küresel 250ms geçit (cover_backfill'teki kanıtlanmış desen).
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
    """Spotify images dizisinden en büyük görsel URL'i (width'e göre)."""
    if not isinstance(images, list) or not images:
        return None
    best = max(
        (im for im in images if isinstance(im, dict) and im.get("url")),
        key=lambda im: im.get("width") or 0,
        default=None,
    )
    return best.get("url") if best else None


def run_artist_image_backfill(
    client: Any,
    settings: Any,
    http: Any,
    batch_limit: int = 60,
    time_budget_s: float = 200.0,
    candidates_rpc: str = _KAT1_RPC,
    grup: KimlikGrubu | None = None,
) -> dict[str, Any]:
    """image_url'i boş sanatçıları Spotify köprüsüyle doldurur.

    batch_limit cover_backfill'den düşük (60): sanatçı başına 2 istek olduğu için
    aynı zaman bütçesinde daha az sanatçı işlenir.

    {outcome, processed, updated, skipped, quota_hit}. outcome:
      'empty'   — doldurulacak sanatçı yok (kuyruk boş, bakım modu).
      'success' — en az bir görsel dolduruldu.
      'partial' — kota/bütçe ile yarıda kaldı (sonraki tur devralır).
      'blocked' — Spotify cooldown aktif, tur atlandı.

    Args:
        candidates_rpc: Aday kuyruğunu veren RPC. Varsayılan **Kat-1**
            (`paket_gorsel_adaylari_sanatci`) — yalnız paketlerde duran
            eksikler. Kör kuyruk (`artist_image_backfill_candidates`) elle
            tek seferlik çalıştırma için hâlâ geçilebilir; ikisi de aynı
            `{id, name, bridge_track_spotify_id}` şeklini döner.
    """
    start = time.monotonic()

    # §1.6 ilk kapı: ortak havuz bloklu ise HİÇ dokunma (token bile alma).
    saglayici = grup.saglayici if grup else _SPOTIFY_PROVIDER
    blocked, remaining = cooldown.is_blocked(client, saglayici)
    if blocked:
        logger.info(
            "Sanatçı görseli: Spotify cooldown aktif (%ds kaldı) — tur atlandı, kota korunuyor",
            remaining,
        )
        return {"outcome": "blocked", "processed": 0, "updated": 0, "skipped": 0}

    # ── Kat-1 kuyruğu (plan 08 §4, migration 0221) ────────────────────────
    # Kör kuyruk yerine yalnız paketlerde duran eksik sanatçılar (ölçüldü:
    # 12 sanatçı, hepsinin köprü şarkısı var). Bkz. `cover_backfill_runner`.
    if grup:
        res = client.rpc(
            "paket_gorsel_adaylari_sanatci_kullanicilar",
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
        logger.warning("Sanatçı görseli: Spotify token alınamadı — tur atlandı")
        return {"outcome": "error", "processed": 0, "updated": 0, "skipped": 0}

    headers = {"Authorization": f"Bearer {token}"}
    processed = 0
    updated = 0
    skipped = 0
    quota_hit = False

    for row in rows:
        if time.monotonic() - start > time_budget_s:
            logger.info("Sanatçı görseli: zaman bütçesi doldu, %d işlendi", processed)
            break

        artist_id = row["id"]
        artist_name = (row["name"] or "").strip()
        bridge_spotify_id = row["bridge_track_spotify_id"]
        name_lower = artist_name.lower()
        processed += 1

        try:
            # 1. Köprü track → sanatçının Spotify id'si.
            def _fetch_track() -> Any:
                r = _paced_get(
                    http,
                    f"https://api.spotify.com/v1/tracks/{bridge_spotify_id}",
                    headers=headers,
                    timeout=10,
                )
                r.raise_for_status()
                return r

            track_resp = _with_retry(_fetch_track)
        except SpotifyQuotaExhausted as exc:
            # Cooldown Spotify'ın istediği süreyle — sabit süre cezayı besler
            # (bkz. cover_backfill_runner, 2026-08-01).
            retry_after = exc.retry_after or 3600.0
            cooldown.set_cooldown(
                client, saglayici, retry_after, reason="artist_image_backfill_429"
            )
            logger.warning(
                "Sanatçı görseli: Spotify kotası tükendi (Retry-After=%.0fs) — cooldown + tur durduruluyor",
                retry_after,
            )
            quota_hit = True
            processed -= 1  # bu satır işlenmeden kaldı
            break
        except Exception:  # noqa: BLE001
            skipped += 1
            continue

        if track_resp is None:
            skipped += 1
            continue

        track_artists = (track_resp.json() or {}).get("artists") or []
        # YANLIŞ GÖRSEL KORUMASI: ada KESİN eşleşen sanatçı yoksa atla (route.ts kuralı).
        matched = next(
            (a for a in track_artists if (a.get("name") or "").strip().lower() == name_lower),
            None,
        )
        if not matched or not matched.get("id"):
            skipped += 1
            continue

        try:
            # 2. Sanatçı detayından görsel.
            def _fetch_artist() -> Any:
                r = _paced_get(
                    http,
                    f"https://api.spotify.com/v1/artists/{matched['id']}",
                    headers=headers,
                    timeout=10,
                )
                r.raise_for_status()
                return r

            artist_resp = _with_retry(_fetch_artist)
        except SpotifyQuotaExhausted as exc:
            retry_after = exc.retry_after or 3600.0
            cooldown.set_cooldown(
                client, saglayici, retry_after, reason="artist_image_backfill_429"
            )
            logger.warning(
                "Sanatçı görseli: Spotify kotası tükendi (Retry-After=%.0fs) — cooldown + tur durduruluyor",
                retry_after,
            )
            quota_hit = True
            break
        except Exception:  # noqa: BLE001
            skipped += 1
            continue

        if artist_resp is None:
            skipped += 1
            continue

        image_url = _largest_image((artist_resp.json() or {}).get("images"))
        if not image_url:
            skipped += 1
            continue

        try:
            client.table("artists").update({"image_url": image_url}).eq("id", artist_id).execute()
            updated += 1
        except Exception:  # noqa: BLE001
            logger.warning("Sanatçı görseli yazımı başarısız: artist=%s", artist_id)
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

"""Az-track'li sanatçı backfill'i — pending kataloğu güvenli yoldan doldur.

Neden var (2026-07-16): ana kuyruk boşaldıktan sonra 5.763 track (3.627 sanatçı)
`genre_pending_reason='artist_too_small'` ile bekliyordu. İki kök neden ölçüldü:
  1. normalize haritası Deezer'ın kendi etiketlerini ("Dans", "Caz"…) tanımıyordu —
     track verisi bulunup boşa düşüyordu (genre_normalize'da düzeltildi).
  2. Az-track koruması, track-çapasından DOĞRULANMIŞ artist.id gelse bile artist
     profilini engelliyordu — hâlbuki id'li yol isim araması yapmaz, korumanın
     savunduğu risk (isim çakışması) o yolda yok (artist_profile.id_only).

Bu runner, ana enrichment kuyruğu BOŞKEN (cron'un aynı 5dk penceresi içinde)
pending track'leri SANATÇI bazında yeniden dener:
  - Her track için mevcut iki-aşamalı Deezer track araması (fold + doğrulama).
  - Sanatçının herhangi bir track'inden doğrulanmış çapa çıkarsa → id_only profil.
  - Sonuç varsa yaz + pending temizle; yoksa 'small_artist_no_match' işaretle —
    backfill onu bir daha SEÇMEZ (sonsuz retry yok), sanatçı ≥5 track olunca
    reactivate RPC'si işareti temizler ve ana akış yeniden dener.

Rate-limit: tüm Deezer çağrıları deezer_genre._paced_get küresel geçidinden
geçer (≥250ms). 429 → cooldown DB'ye yazılır, tur kısmi sonuçla biter (§1.6).
"""
from __future__ import annotations

import logging
import time
from typing import Any

from app.services import cooldown
from app.services.artist_profile import get_or_build_artist_profile, is_artist_match
from app.services.genre_errors import RateLimitError
from app.pipeline.genre_runner import (
    _TRACK_SLOTS,
    _build_from_artist_base,
    build_track_genre_data,
    fill_empty_slots,
)

logger = logging.getLogger("rosso.worker.small_artist_backfill")

PENDING_REASON = "artist_too_small"
NO_MATCH_REASON = "small_artist_no_match"


def run_small_artist_backfill(
    client: Any,
    settings: Any,
    *,
    artist_limit: int = 60,
    time_budget_s: float = 200.0,
) -> dict[str, Any]:
    """Bir tur backfill: en fazla artist_limit sanatçı, süre bütçesi içinde.

    Döner: {outcome, artists, updated, no_match} — updated=yazılan track sayısı.
    """
    deadline = time.monotonic() + time_budget_s

    dz_blocked, _ = cooldown.is_blocked(client, "deezer")
    if dz_blocked:
        return {"outcome": "blocked", "artists": 0, "updated": 0, "no_match": 0}

    # Pending track'leri sanatçı-bitişik sırada çek; sanatçı başına ≤4 track
    # olduğu için artist_limit*4 satır üst sınırdır.
    res = (
        client.table("tracks")
        .select("id, title, artists")
        .eq("genre_pending_reason", PENDING_REASON)
        .order("artists")
        .limit(artist_limit * 4)
        .execute()
    )
    rows = res.data or []
    if not rows:
        return {"outcome": "empty", "artists": 0, "updated": 0, "no_match": 0}

    # Sanatçıya göre grupla, ilk artist_limit sanatçıyı al.
    by_artist: dict[str, list[dict]] = {}
    for r in rows:
        artists = r.get("artists") or []
        artist = artists[0] if artists else ""
        if not artist:
            continue
        if artist not in by_artist and len(by_artist) >= artist_limit:
            continue
        by_artist.setdefault(artist, []).append(r)

    lastfm_key = getattr(settings, "lastfm_api_key", "") or ""
    updated = 0
    no_match = 0
    artists_done = 0

    import httpx

    with httpx.Client(timeout=httpx.Timeout(20.0)) as http:
        for artist, tracks in by_artist.items():
            if time.monotonic() > deadline:
                break  # kalan sanatçılar pending kalır, sonraki tur devralır

            # 1) Her track için iki-aşamalı Deezer track araması + çapa toplama.
            enriched: list[tuple[dict, dict]] = []  # (row, track_data)
            best_id: int | None = None
            best_name: str | None = None
            try:
                for row in tracks:
                    data, anchor, anchor_id, rl = build_track_genre_data(
                        artist, row.get("title") or "", lastfm_key=lastfm_key, http=http
                    )
                    if rl is not None:
                        provider, retry_after = rl
                        cooldown.set_cooldown(
                            client, provider, retry_after or 60.0, reason="genre_429"
                        )
                        # Kısmi sonuçla çık — bu sanatçının track'leri pending kalır.
                        return {
                            "outcome": "rate_limited",
                            "artists": artists_done, "updated": updated, "no_match": no_match,
                        }
                    enriched.append((row, data))
                    if best_id is None and anchor and anchor_id and is_artist_match(artist, anchor):
                        best_id, best_name = anchor_id, anchor

                # 2) Doğrulanmış çapa varsa id_only profil (isim araması YOK).
                profile: dict = {"slots": [], "weights": []}
                if best_id:
                    profile = get_or_build_artist_profile(
                        artist, client, http, lastfm_key,
                        query_name=best_name, query_id=best_id, id_only=True,
                    )
            except RateLimitError as exc:
                cooldown.set_cooldown(
                    client, exc.provider, exc.retry_after or 60.0, reason="genre_429"
                )
                return {
                    "outcome": "rate_limited",
                    "artists": artists_done, "updated": updated, "no_match": no_match,
                }
            except Exception:  # noqa: BLE001
                logger.warning("Backfill sanatçı hatası (atlandı): %s", artist)
                continue

            # 3) Yazım: track verisi > profil tabanı > no_match işareti.
            for row, data in enriched:
                track_had_data = bool(data.get("slots"))
                if track_had_data and len(data["slots"]) < _TRACK_SLOTS and profile.get("slots"):
                    data = fill_empty_slots(data, profile)
                elif not track_had_data and profile.get("slots"):
                    data = _build_from_artist_base(profile)

                if data.get("slots"):
                    source = "multi_source" if track_had_data else "artist_profile"
                    try:
                        client.table("tracks").update({
                            "genres": data["slots"],
                            "genre_data": data,
                            "genre_source": source,
                            "genre_pending_reason": None,
                        }).eq("id", row["id"]).execute()
                        updated += 1
                    except Exception:  # noqa: BLE001
                        logger.warning("Backfill yazım hatası: track=%s", row["id"])
                else:
                    # Kalıcı lookup_failed DEĞİL: sanatçı ≥5 track olunca
                    # reactivate RPC'si bu işareti temizler, ana akış dener.
                    try:
                        client.table("tracks").update(
                            {"genre_pending_reason": NO_MATCH_REASON}
                        ).eq("id", row["id"]).execute()
                        no_match += 1
                    except Exception:  # noqa: BLE001
                        logger.warning("Backfill no_match yazım hatası: track=%s", row["id"])

            artists_done += 1

    logger.info(
        "Backfill turu: %s sanatçı, %s track yazıldı, %s no_match",
        artists_done, updated, no_match,
    )
    return {"outcome": "success", "artists": artists_done, "updated": updated, "no_match": no_match}

"""Mood haftalık senkron — Spotify playlist'ini yeni pakete göre günceller.

Efendim'in kararı (2026-08-11): "senkron et seçeneğini de açarak haftalık
olarak playlistin güncellenmesini seçebilir."

Akış: `mood_pkg_runner`'DAN SONRA çalışır (paket taze olmalı). Adayları
`get_mood_weekly_sync_candidates()` RPC'sinden okur (yalnız
`weekly_sync_enabled=true` VE daha önce Spotify'a aktarılmış — `exported_
playlist_id` dolu — kayıtlar). Her aday için `mood_pkg`'dan taze payload'ı
okuyup Spotify'ın `PUT /playlists/{id}/tracks` (replace) uç noktasına yazar.

⚠ TAZELİK: haftada bir mi çalışacağı burada DEĞİL, cron zamanlamasında
kontrol edilir (haftalık zamanlama). Runner'ın kendisi
"bu turda kim hazır" diye sormaz, günlük de çağrılsa yalnız adayları işler;
zamanlama sorumluluğu cron tarafında.

⚠ İZOLE: bir kullanıcının/mood'un senkron hatası diğerlerini durdurmaz
(pipeline deseni, match_batch_runner ile aynı).

Token: `app.services.spotify_token.get_valid_spotify_token` — TS
`ensureValidToken`'ın Python karşılığı, gerekirse otomatik yeniler.
"""
from __future__ import annotations

import logging
from typing import Any

from app.services.spotify_token import get_valid_spotify_token

logger = logging.getLogger("rosso.worker.mood_weekly_sync_runner")

#: Spotify tek istekte max 100 URI kabul eder (replace dahil). Mood limiti
#: 50 (`MOOD_TRACK_LIMIT`), bugün için güvenli.
#:
#: ⚠ 2026-08-26 düzeltmesi: burada eskiden `payload[:_MAX_TRACKS_PER_REPLACE]`
#: ile SESSİZCE kırpılıyordu — yorum "görünür hata" dese de kod tam tersini
#: yapıyordu (TS karşılığı `replaceTracksOnSpotify` doğru davranıyor: limit
#: aşılırsa `too_many_tracks_for_single_replace` döner). MOOD_TRACK_LIMIT
#: ileride 100'ü aşarsa kullanıcı playliste eksik şarkı yazıldığını asla
#: görmezdi. Şimdi limit aşılan aday SKIP edilir ve loglanır — TS ile aynı
#: disiplin: eksik veri yazmaktansa hiç yazmamak.
_MAX_TRACKS_PER_REPLACE = 100


def run_mood_weekly_sync(client: Any, crypto_key: str, http: Any) -> dict[str, Any]:
    """Senkron açık mood'ların Spotify playlist'ini taze pakete göre günceller.

    {outcome, synced, skipped, errors} döner.
    """
    try:
        res = client.rpc("get_mood_weekly_sync_candidates", {}).execute()
        candidates = res.data or []
    except Exception as exc:  # noqa: BLE001
        logger.exception("mood_weekly_sync: aday sorgusu başarısız")
        return {"outcome": "error", "synced": 0, "skipped": 0, "errors": 0, "error": str(exc)[:300]}

    if not candidates:
        return {"outcome": "empty", "synced": 0, "skipped": 0, "errors": 0}

    synced = 0
    skipped = 0
    errors = 0

    for row in candidates:
        user_id = row.get("user_id")
        mood_key = row.get("mood_key")
        playlist_id = row.get("exported_playlist_id")
        if not user_id or not mood_key or not playlist_id:
            skipped += 1
            continue

        try:
            # Taze paketten spotify_id'leri oku. Migration 0271'den sonra
            # `mood_pkg.payload` doğrudan `spotify_id` taşıyor (`build_mood_pkg`
            # artık `mood_playlist` RPC'sinin `with ordinality` sırasını
            # koruyarak yazıyor) — önceden ayrı bir `tracks` sorgusuyla
            # track_id → spotify_id çevrimi yapılıyordu, artık gerekmiyor.
            pkg_res = (
                client.table("mood_pkg")
                .select("payload")
                .eq("user_id", user_id)
                .eq("mood_key", mood_key)
                .maybe_single()
                .execute()
            )
            payload = (pkg_res.data or {}).get("payload") or []
            spotify_ids = [t.get("spotify_id") for t in payload if t.get("spotify_id")]

            if not spotify_ids:
                skipped += 1
                continue

            # ⚠ Kırpma YASAK — kullanıcı playliste eksik şarkı yazıldığını
            # hiç görmeden "senkronlandı" sanırdı. TS `replaceTracksOnSpotify`
            # ile aynı disiplin: sınır aşılırsa hiç yazma, görünür şekilde atla.
            if len(spotify_ids) > _MAX_TRACKS_PER_REPLACE:
                skipped += 1
                logger.warning(
                    "mood senkron atlandı (limit aşıldı): user=%s mood=%s track_count=%d > %d",
                    user_id, mood_key, len(spotify_ids), _MAX_TRACKS_PER_REPLACE,
                )
                continue

            token = get_valid_spotify_token(client, user_id, crypto_key, http)
            if not token:
                skipped += 1
                continue

            uris = [f"spotify:track:{sid}" for sid in spotify_ids]
            resp = http.put(
                f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks",
                json={"uris": uris},
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code >= 300:
                errors += 1
                logger.warning(
                    "mood senkron başarısız user=%s mood=%s status=%s",
                    user_id, mood_key, resp.status_code,
                )
                continue

            synced += 1
        except Exception as exc:  # noqa: BLE001
            errors += 1
            logger.warning(
                "mood senkron exception user=%s mood=%s: %s",
                user_id, mood_key, str(exc)[:200],
            )

    # Diğer paket runner'larıyla (mood_pkg_runner, match_batch_runner) aynı
    # sözleşme: hiçbir şey senkron edilmediyse (hepsi atlandıysa) 'success'
    # yanıltıcı olur — 'empty' gerçek durumu söyler.
    if synced == 0 and errors > 0:
        outcome = "error"
    elif errors > 0:
        outcome = "partial"
    elif synced == 0:
        outcome = "empty"
    else:
        outcome = "success"

    return {"outcome": outcome, "synced": synced, "skipped": skipped, "errors": errors}

"""Mood (an) listeleri paketi üretimi (Aşama 3 · paket #6).

Belge: docs/reference/performans-olcumleri.md §12.4

`mood_pkg` tablosunu doldurur. `/mood/[key]` detay sayfasını besler.

TAZELİK: B · Günlük.

⚠ ANAHTAR `user × mood`: kullanıcı başına BEŞ paket. Bu, Aşama 3'ün en pahalı
gece yükü — 10.000 kullanıcıda günde 50.000 paket. Kuyruk/batch gerekliliği
ölçekte burada başlar (§Cron bütçesi).

⚠ `/api/mood/create-playlist` PAKETE BAĞLANMADI (bilinçli): o uç kullanıcının
Spotify hesabına GERÇEK playlist yazıyor. Pakete bağlansaydı kullanıcı "şu an"
değil "gece yarısı üretilmiş" listeyi Spotify'a yazardı — dış dünyaya taşan,
geri alınamaz bir bayatlık. Sayfada bayat liste kabul edilebilir; Spotify'a
bayat yazmak değil.

⚠ `is_test` politikası (Efendim): test profilleri paketlerini BİR KEZ alır,
cron onlara DOKUNMAZ → aday listesi `recap_real_user_ids()` (migration 0129).
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("rosso.worker.mood_pkg_runner")

#: Paket kaç saatten sonra bayat sayılır. Diğer paket runner'larıyla aynı
#: gerekçe: cron gecikirse bir gün atlanmasın diye 24 değil 20.
STALE_AFTER_HOURS = 20

#: Sabit anlar. ⚠ `src/lib/analytics/mood.ts` → `MOODS` ile AYNI olmalı;
#: `build_mood_pkg` geçersiz anahtarı reddeder (0169, 0269, 0271), yani
#: ayrışma sessiz kalmaz — cron hata verir.
#: 2026-08-11 (Efendim, mood kataloğu genişlemesi): `su_siralar` +
#: `dagittik_galiba` eklendi (migration 0269).
#: 2026-08-11 gece oturumu (Efendim: "karakteristik 10 adet playlist"):
#: `takinti`, `kesif`, `arsiv` eklendi — katalog 10'a tamamlandı
#: (migration 0271). Aynı migration `build_mood_pkg` payload'ına
#: `spotify_id` ekledi; bu runner ayrıca dokunmuyor, RPC'den geliyor.
MOOD_KEYS = (
    "gece3",
    "gecesurus",
    "su_siralar",
    "gunduz",
    "sabah",
    "odak",
    "takinti",
    "dagittik_galiba",
    "kesif",
    "arsiv",
)


def run_mood_pkg(client: Any, force: bool = False) -> dict[str, Any]:
    """Bayat paketi olan kullanıcılar için `build_mood_pkg` çağırır (5 mood).

    {outcome, users_processed, moods_written, skipped, empty, errors} döner.
    Bir mood'un hatası diğer dördünü DÜŞÜRMEZ; bir kullanıcının hatası
    diğerlerini durdurmaz (çift izolasyon).

    Args:
        force: True ise tazelik kontrolü atlanır (şema değişikliği sonrası).
    """
    # ⚠ Aday listesi DB tarafında (CLAUDE.md §4.3): Supabase REST ~1000 satırda
    # sessizce kırpar. `recap_real_user_ids()` hem DISTINCT yapar hem test
    # profillerini dışlar.
    try:
        res = client.rpc("recap_real_user_ids", {}).execute()
        user_ids = sorted({u for u in (res.data or []) if u})
    except Exception as exc:  # noqa: BLE001
        logger.exception("mood_pkg: aday sorgusu başarısız")
        return {"outcome": "error", "users_processed": 0, "moods_written": 0,
                "skipped": 0, "empty": 0, "errors": 0, "error": str(exc)[:300]}

    if not user_ids:
        return {"outcome": "empty", "users_processed": 0, "moods_written": 0,
                "skipped": 0, "empty": 0, "errors": 0}

    processed = 0
    moods_written = 0
    skipped = 0
    empty_moods = 0
    errors = 0

    for uid in user_ids:
        if not force and not _needs_refresh(client, uid):
            skipped += 1
            continue

        user_had_write = False
        for mood_key in MOOD_KEYS:
            try:
                res = client.rpc(
                    "build_mood_pkg", {"p_user_id": uid, "p_mood_key": mood_key}
                ).execute()

                # RPC false dönerse: bu mood için yeterli veri yok. Hata DEĞİL —
                # paket yazılmadı, UI "hazırlanıyor" gösterecek.
                if res.data is False:
                    empty_moods += 1
                else:
                    moods_written += 1
                    user_had_write = True
            except Exception as exc:  # noqa: BLE001
                errors += 1
                logger.warning(
                    "mood_pkg başarısız user=%s mood=%s: %s", uid, mood_key, str(exc)[:200]
                )

        if user_had_write:
            processed += 1

    if processed == 0 and errors > 0:
        outcome = "error"
    elif errors > 0:
        outcome = "partial"
    elif processed == 0:
        outcome = "empty"  # hepsi taze ya da verisiz — normal durum
    else:
        outcome = "success"

    return {
        "outcome": outcome,
        "users_processed": processed,
        "moods_written": moods_written,
        "skipped": skipped,
        "empty": empty_moods,
        "errors": errors,
    }


def _needs_refresh(client: Any, user_id: str) -> bool:
    """Bu kullanıcının paketleri bayat mı (20 saatten eski) ya da hiç yok mu?

    ⚠ Kullanıcı başına TEK kontrol: beş mood aynı cron turunda yazıldığı için
    hepsinin damgası aynıdır. Mood başına sorgu atmak 5 kat REST isteği demekti.
    """
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=STALE_AFTER_HOURS)).isoformat()

    try:
        res = (
            client.table("mood_pkg")
            .select("generated_at")
            .eq("user_id", user_id)
            .gte("generated_at", cutoff)
            .limit(1)
            .execute()
        )
    except Exception:  # noqa: BLE001
        # Kontrol edilemiyorsa ÜRET — bayat veri göstermektense fazladan
        # hesaplamak yeğdir.
        logger.warning("mood_pkg: tazelik kontrolü başarısız user=%s → üretilecek", user_id)
        return True

    return not (res.data or [])

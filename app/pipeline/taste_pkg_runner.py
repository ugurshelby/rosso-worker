"""Tür DNA + Kronotip paketi üretimi (Aşama 3 · paket #2).

Belge: docs/reference/performans-anatomisi.md §12.4

`user_taste_pkg` tablosunu doldurur. Üç yüzeyi birden besler:
    /taste     → Tür DNA + Kronotip ısı haritası (birincil veri)
    /profile   → zirve saat + 24 kutu (yan bölüm)
    /u/[user]  → aynısı, başkasının profilinde

TAZELİK: B · Günlük. Tür dağılımı ve saat kutuları günlük ölçekte değişir;
gecelik tazeleme kullanıcının fark edeceği bir gecikme yaratmaz.

⚠ TOPLANAMAZ (delta YOK): Tür sayımı `unnest(genres)` üzerinden gider ve bir
şarkı birden çok türe sayılır. Delta yazmak yanlış toplama riski taşır —
"bugün eklenen 62 satır" ile toplam arasında birebir ilişki yok. Tam hesap
423 ms, kullanıcı başına günde bir kez; kabul edilebilir.

⚠ `is_test` politikası (Efendim): test profilleri paketlerini BİR KEZ alır,
cron onlara DOKUNMAZ → aday listesi `recap_real_user_ids()` (migration 0129).
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("rosso.worker.taste_pkg_runner")

#: Paket kaç saatten sonra bayat sayılır. Günlük cron için 20 saat: cron her
#: gece aynı saatte koşmayabilir (deploy gecikmesi, kuyruk), 24 yazılsaydı bir
#: gün atlanabilirdi.
STALE_AFTER_HOURS = 20


def run_taste_pkg(client: Any, force: bool = False) -> dict[str, Any]:
    """Bayat paketi olan kullanıcılar için `build_user_taste_pkg` çağırır.

    {outcome, users_processed, skipped, empty, errors} döner. Bir kullanıcının
    hatası diğerlerini durdurmaz (izole) — `journey_pkg_runner` ile aynı desen.

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
        logger.exception("taste_pkg: aday sorgusu başarısız")
        return {"outcome": "error", "users_processed": 0, "skipped": 0,
                "empty": 0, "errors": 0, "error": str(exc)[:300]}

    if not user_ids:
        return {"outcome": "empty", "users_processed": 0, "skipped": 0,
                "empty": 0, "errors": 0}

    processed = 0
    skipped = 0
    empty_users = 0
    errors = 0

    for uid in user_ids:
        try:
            if not force and not _needs_refresh(client, uid):
                skipped += 1
                continue

            res = client.rpc("build_user_taste_pkg", {"p_user_id": uid}).execute()

            # RPC false dönerse: kullanıcının tür/saat verisi yok. Hata DEĞİL —
            # paket yazılmadı, UI "hazırlanıyor" gösterecek.
            if res.data is False:
                empty_users += 1
            else:
                processed += 1
        except Exception as exc:  # noqa: BLE001
            errors += 1
            logger.warning("taste_pkg başarısız user=%s: %s", uid, str(exc)[:200])

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
        "skipped": skipped,
        "empty": empty_users,
        "errors": errors,
    }


def _needs_refresh(client: Any, user_id: str) -> bool:
    """Bu kullanıcının paketi bayat mı (20 saatten eski) ya da hiç yok mu?"""
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=STALE_AFTER_HOURS)).isoformat()

    try:
        res = (
            client.table("user_taste_pkg")
            .select("generated_at")
            .eq("user_id", user_id)
            .gte("generated_at", cutoff)
            .limit(1)
            .execute()
        )
    except Exception:  # noqa: BLE001
        # Kontrol edilemiyorsa ÜRET — bayat veri göstermektense fazladan
        # hesaplamak yeğdir.
        logger.warning("taste_pkg: tazelik kontrolü başarısız user=%s → üretilecek", user_id)
        return True

    return not (res.data or [])

"""Dashboard StatBar + Profil tür haritası paketi üretimi (Aşama 3 · paket #4).

Belge: docs/reference/performans-anatomisi.md §12.4

`user_stats_pkg` tablosunu doldurur. Dashboard StatBar'ın dört kutusunu
(şarkı / sanatçı / tür / dinleme süresi) ve Profil sayfasının tür haritasını
besler — iki yüzey, tek paket.

TAZELİK: B · Günlük.

⚠ YALNIZ TAM GEÇMİŞ (Faz 3+): Kaynak RPC'ler `p_source` parametresi alıyor ve
iki yol FARKLI sonuç veriyor (ölçüldü: Faz 3+ 47 tür / 15.894 şarkı, Faz 2
15 tür / 411 şarkı). Efendim'in kararı — "karmaşıklaşmasın, hızlansın":
Faz 2 (`api_realtime`) yolu RPC'de KALIR. Gerekçe ölçümle: o pencere ~20 gün
ve küçük (411 şarkı, 109 ms), üstelik günde 60-180 satır büyüdüğü için
paketlense bile sürekli bayatlardı. `user x source` seçilseydi gece yükü
2 kat olurdu — zaten ucuz olan yol için.

⚠ Delta YAZILMADI: §12.4-B "✅ delta" diyor ama §12.4-C'nin kendi uyarısı bunu
çürütüyor — "TEKİL SAYIMLAR TOPLANAMAZ". Bu paketin ürünü tam da tekil sayımlar
(kaç farklı şarkı/sanatçı/tür). Delta için gün bazlı track_id kümesi gerekirdi;
ölçülen maliyet (121-1276 ms) bunu haklı çıkarmıyor. Tam hesap.

⚠ `is_test` politikası (Efendim): test profilleri paketlerini BİR KEZ alır,
cron onlara DOKUNMAZ → aday listesi `recap_real_user_ids()` (migration 0129).
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("rosso.worker.stats_pkg_runner")

#: Paket kaç saatten sonra bayat sayılır. Günlük cron için 20 saat: cron her
#: gece aynı saatte koşmayabilir (deploy gecikmesi, kuyruk), 24 yazılsaydı bir
#: gün atlanabilirdi. `pattern_pkg_runner` ile aynı gerekçe.
STALE_AFTER_HOURS = 20


def run_stats_pkg(client: Any, force: bool = False) -> dict[str, Any]:
    """Bayat paketi olan kullanıcılar için `build_user_stats_pkg` çağırır.

    {outcome, users_processed, skipped, empty, errors} döner. Bir kullanıcının
    hatası diğerlerini durdurmaz (izole) — `pattern_pkg_runner` ile aynı desen.

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
        logger.exception("stats_pkg: aday sorgusu başarısız")
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

            res = client.rpc("build_user_stats_pkg", {"p_user_id": uid}).execute()

            # RPC false dönerse: kullanıcının dinleme verisi yok. Hata DEĞİL —
            # paket yazılmadı, UI "hazırlanıyor" gösterecek.
            if res.data is False:
                empty_users += 1
            else:
                processed += 1
        except Exception as exc:  # noqa: BLE001
            errors += 1
            logger.warning("stats_pkg başarısız user=%s: %s", uid, str(exc)[:200])

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
            client.table("user_stats_pkg")
            .select("generated_at")
            .eq("user_id", user_id)
            .gte("generated_at", cutoff)
            .limit(1)
            .execute()
        )
    except Exception:  # noqa: BLE001
        # Kontrol edilemiyorsa ÜRET — bayat veri göstermektense fazladan
        # hesaplamak yeğdir.
        logger.warning("stats_pkg: tazelik kontrolü başarısız user=%s → üretilecek", user_id)
        return True

    return not (res.data or [])

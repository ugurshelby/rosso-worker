"""Enrichment cron entry — cooldown bak, 1 genre batch işle, çık.
Çalıştırma: python -m app.cron.enrichment."""
from __future__ import annotations

import logging
import sys

from app.cron._logging import log_run, setup_logging

setup_logging()
logger = logging.getLogger("rosso.worker.cron.enrichment")


def main() -> int:
    from app.config import get_settings
    from app.db import get_client
    from app.pipeline.genre_runner import run_one_genre_batch
    from app.services import run_log

    client = get_client()
    settings = get_settings()
    try:
        # batch 40→200 (2026-07-16): cron minimumu 5 dk, tur 10 sn sürüyordu
        # — pencerenin %3'ü. Deezer artık _paced_get ile ~4 istek/sn'ye sabit
        # (ölçülen limit 50/5sn); 200 track ≈ 130-160 sn, 240 sn bütçeye sığar.
        # Bütçe dolarsa kalan track'lere dokunulmaz, sonraki tur devralır.
        result = run_one_genre_batch(client, settings, batch_limit=200, time_budget_s=240.0)

        # Ana kuyruk boşsa, az-track backfill'i yalnız günde bir kez (24 saatte bir)
        # çalıştır — aksi halde her 5 dakikada 200 sn CPU harcayıp çalışma kotasını tüketir.
        if result["outcome"] == "empty":
            from app.cron._dispatch import should_run_interval
            if should_run_interval(client, "enrichment", min_hours=24.0):
                from app.pipeline.small_artist_backfill import run_small_artist_backfill

                bf = run_small_artist_backfill(
                    client, settings, artist_limit=60, time_budget_s=120.0
                )
                result["backfill"] = bf
                if bf.get("updated") or bf.get("no_match"):
                    result["outcome"] = "success"
                    result["updated"] = bf.get("updated", 0)
            else:
                result["backfill"] = {"status": "skipped_interval"}

        # ⚠ `backfill` bir DICT — elle `%s` ile basıldığında `repr()` uygulanıp
        # tek tırnaklı, GEÇERSİZ JSON üretiyordu (2026-08-01'de yakalandı).
        log_run(
            logger, "enrichment",
            status=result["outcome"],
            processed=result.get("processed", 0),
            updated=result.get("updated", 0),
            skipped_deadline=result.get("skipped_deadline", 0),
            backfill=result.get("backfill", {}),
        )
        run_log.record_run(client, "enrichment", outcome=result["outcome"], stats=result)
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("Enrichment başarısız")
        run_log.record_run(client, "enrichment", outcome="error", error=str(exc)[:500])
        return 1


if __name__ == "__main__":
    sys.exit(main())

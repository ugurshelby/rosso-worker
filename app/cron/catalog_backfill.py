"""Katalog dolgu cron'u — ISRC + duration_ms + album + release_year.

Railway: python -m app.cron.catalog_backfill

Kuyruk boşalınca `empty` döner ve hiçbir Spotify isteği atmaz → cron sonsuza
kadar çalışsa da maliyet sıfıra iner. Yeni track'ler (recently_played) geldikçe
kuyruğa girer ve otomatik dolgulanır — yani bu cron hem TEK SEFERLİK dolgu hem
SÜREKLİ bakım yapar.
"""
from __future__ import annotations

import logging
import sys

from app.cron._logging import setup_logging

setup_logging()
logger = logging.getLogger("rosso.worker.cron.catalog_backfill")


def main() -> int:
    from app.config import get_settings
    from app.db import get_client
    from app.cron._gruplu import gruplu_calistir
    from app.pipeline.catalog_backfill import run_one_catalog_batch
    from app.services import run_log

    client = get_client()
    settings = get_settings()
    try:
        # Her kullanıcı KENDİ Spotify app'inin kotasıyla (2026-09-23).
        result = gruplu_calistir(
            client, settings,
            lambda grup, kalan: run_one_catalog_batch(client, settings, max_batches=8, grup=grup),
            toplam_butce_s=240.0,
        )
        logger.info(
            '{"run":"catalog_backfill","status":"%s","processed":%d,"updated":%d}',
            result["outcome"], result.get("processed", 0), result.get("updated", 0),
        )
        run_log.record_run(
            client, "catalog_backfill",
            outcome=result["outcome"],
            stats=result,
            error=result.get("error"),
        )
        # partial/error de 0 döner: cron'un kendisi çökmedi, iş kısmen yapıldı.
        # Railway'de exit≠0 alarm üretir — kota tükenmesi alarm DEĞİLDİR.
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("Katalog dolgusu başarısız")
        run_log.record_run(
            client, "catalog_backfill", outcome="error", error=str(exc)[:500]
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())

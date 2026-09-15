"""ISRC dolgu cron'u — Deezer öncelikli (FAZ ISRC-D).

Railway: python -m app.cron.isrc_backfill

Kuyruk boşalınca `empty` döner ve hiçbir Deezer isteği atmaz → cron sürekli
çalışsa da maliyet sıfıra iner. Yeni track'ler geldikçe kuyruğa girer ve
otomatik dolgulanır — hem tek seferlik dolgu hem sürekli bakım.

Eski Spotify-tabanlı `app.cron.catalog_backfill` DEVRE DIŞI kalmaya devam
ediyor (aylık, fiilen durmuş); bu cron ondan bağımsızdır.
"""
from __future__ import annotations

import logging
import sys

from app.cron._logging import setup_logging

setup_logging()
logger = logging.getLogger("rosso.worker.cron.isrc_backfill")


def main() -> int:
    from app.db import get_client
    from app.pipeline.isrc_backfill import run_one_isrc_batch
    from app.services import run_log

    client = get_client()
    try:
        result = run_one_isrc_batch(client)
        logger.info(
            '{"run":"isrc_backfill","status":"%s","processed":%d,"updated":%d,"found":%d}',
            result["outcome"], result.get("processed", 0),
            result.get("updated", 0), result.get("found", 0),
        )
        run_log.record_run(
            client, "isrc_backfill",
            outcome=result["outcome"],
            stats=result,
            error=result.get("error"),
        )
        # partial/blocked de 0 döner: cron çökmedi, iş kısmen yapıldı/ertelendi.
        # Railway'de exit≠0 alarm üretir — kota beklemesi alarm DEĞİLDİR.
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("ISRC dolgusu başarısız")
        run_log.record_run(
            client, "isrc_backfill", outcome="error", error=str(exc)[:500]
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())

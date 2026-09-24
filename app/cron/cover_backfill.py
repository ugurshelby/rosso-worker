"""Kapak dolgusu cron entry — image_url'i boş track'lere Spotify kapağı.
Railway: python -m app.cron.cover_backfill.

⚠ Bu cron gerçek Spotify istekleri atar (§1.6). Railway'de servisi Efendim'in
onayı/gözetimiyle kurulur — küçük batch + zaman bütçesi ile turlar hâlinde
ilerler, tek gecede binlerce istek atmaz.
"""
from __future__ import annotations

import logging
import sys

from app.cron._logging import log_run, setup_logging

setup_logging()
logger = logging.getLogger("rosso.worker.cron.cover_backfill")


def main() -> int:
    import httpx

    from app.config import get_settings
    from app.db import get_client
    from app.cron._gruplu import gruplu_calistir
    from app.pipeline.cover_backfill_runner import run_cover_backfill
    from app.services import run_log

    client = get_client()
    settings = get_settings()
    try:
        with httpx.Client(timeout=15) as http:
            # Her kullanıcı KENDİ Spotify app'inin kotasıyla (2026-09-23).
            result = gruplu_calistir(
                client, settings,
                lambda grup, kalan: run_cover_backfill(
                    client, settings, http, batch_limit=100,
                    time_budget_s=min(kalan, 200.0), grup=grup,
                ),
                toplam_butce_s=200.0,
            )
        # ⚠ `quota_hit` bir BOOL — elle `%s` ile basıldığında Python'un
        # `True`/`False`'ı yazılıyordu; JSON `true`/`false` bekler (2026-08-01).
        log_run(
            logger, "cover_backfill",
            status=result["outcome"],
            processed=result.get("processed", 0),
            updated=result.get("updated", 0),
            skipped=result.get("skipped", 0),
            quota_hit=result.get("quota_hit", False),
        )
        run_log.record_run(client, "cover_backfill", outcome=result["outcome"], stats=result)
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("Kapak dolgusu başarısız")
        run_log.record_run(client, "cover_backfill", outcome="error", error=str(exc)[:500])
        return 1


if __name__ == "__main__":
    sys.exit(main())

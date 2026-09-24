"""Deezer kapak dolgusu cron entry (2026-09-24) — Spotify'ı olmayan kullanıcıların kapakları.

python -m app.cron.deezer_cover_backfill

Kuyruk boşalınca `empty` döner ve hiçbir Deezer isteği atmaz. Ayrıntı ve doğrulama
kuralları: `app/pipeline/deezer_cover_runner.py`.
"""
from __future__ import annotations

import logging
import sys

from app.cron._logging import log_run, setup_logging

setup_logging()
logger = logging.getLogger("rosso.worker.cron.deezer_cover_backfill")


def main() -> int:
    import httpx

    from app.db import get_client
    from app.pipeline.deezer_cover_runner import run_deezer_cover_backfill
    from app.services import run_log

    client = get_client()
    try:
        with httpx.Client(timeout=15) as http:
            result = run_deezer_cover_backfill(client, http, batch_limit=80, time_budget_s=150.0)
        log_run(
            logger, "deezer_cover_backfill",
            status=result["outcome"],
            processed=result.get("processed", 0),
            updated=result.get("updated", 0),
            skipped=result.get("skipped", 0),
            quota_hit=result.get("quota_hit", False),
        )
        run_log.record_run(client, "deezer_cover_backfill", outcome=result["outcome"], stats=result)
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("Deezer kapak dolgusu başarısız")
        run_log.record_run(client, "deezer_cover_backfill", outcome="error", error=str(exc)[:500])
        return 1


if __name__ == "__main__":
    sys.exit(main())

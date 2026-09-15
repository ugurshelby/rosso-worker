"""Worker-fast dispatcher — sık aralıklı arka plan işleri.

Railway: python -m app.cron.fast
Schedule önerisi: */5 * * * * (her 5 dk)

Sıra:
  1. export (kuyruk varsa zaman bütçesi içinde birden fazla iş)
  2. spotify_recently_played
  3. enrichment
  4. isrc_backfill
  5. playlist_refresh (son başarılı turdan ≥12 saat geçtiyse)
"""
from __future__ import annotations

import logging
import sys

from app.cron._dispatch import run_step, should_run_interval
from app.cron._logging import log_run, setup_logging
from app.cron import enrichment as enrichment_cron
from app.cron import export as export_cron
from app.cron import isrc_backfill as isrc_cron
from app.cron import playlist_refresh as playlist_cron
from app.cron import spotify_recently_played as recently_played_cron

setup_logging()
logger = logging.getLogger("rosso.worker.cron.fast")

PLAYLIST_MIN_HOURS = 12.0
EXPORT_BURST_BUDGET_S = 300.0


def main() -> int:
    from app.db import get_client

    client = get_client()
    log_run(logger, "worker_fast", status="start")

    export_cron.run_export_burst(time_budget_s=EXPORT_BURST_BUDGET_S)
    run_step("spotify_recently_played", recently_played_cron.main)
    run_step("enrichment", enrichment_cron.main)
    run_step("isrc_backfill", isrc_cron.main)

    if should_run_interval(client, "playlist_refresh", min_hours=PLAYLIST_MIN_HOURS):
        run_step("playlist_refresh", playlist_cron.main)
    else:
        log_run(logger, "playlist_refresh", status="skipped", reason="interval_not_elapsed")

    log_run(logger, "worker_fast", status="done")
    return 0


if __name__ == "__main__":
    sys.exit(main())

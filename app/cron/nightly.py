"""Worker-nightly dispatcher — gece toplu paket + bakım işleri.

Çalıştırma: python -m app.cron.nightly
Schedule önerisi: 0 3 * * * (günde 1, 03:00 UTC = 06:00 TR)

Sıra:
  1. taste_refresh (yalnız Pazartesi — haftalık)
  2. journey_pkg → taste_pkg → pattern_pkg → stats_pkg → period_pkg
  3. mood_pkg (+ içindeki mood_weekly_sync)
  4. recap_refresh
5. match_batch — kişisel Rosso'da atlanır
6. auto_playlist (iç kapılar aylık üretimi yönetir)
7. account_purge (yıkıcı — en sonda)
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone

from app.cron._dispatch import run_step
from app.cron._logging import log_run, setup_logging
from app.cron import account_purge as purge_cron
from app.cron import auto_playlist as auto_playlist_cron
from app.cron import journey_pkg as journey_cron
from app.cron import log_cleanup as log_cleanup_cron
from app.cron import mood_pkg as mood_cron
from app.cron import pattern_pkg as pattern_cron
from app.cron import period_pkg as period_cron
from app.cron import recap_refresh as recap_cron
from app.cron import stats_pkg as stats_cron
from app.cron import taste_pkg as taste_pkg_cron
from app.cron import taste_refresh as taste_refresh_cron

setup_logging()
logger = logging.getLogger("rosso.worker.cron.nightly")

# Pazartesi = 0 (UTC). Taste refresh haftalık.
TASTE_REFRESH_WEEKDAY = 0


def main() -> int:
    now = datetime.now(timezone.utc)
    log_run(logger, "worker_nightly", status="start", weekday=now.weekday())

    if now.weekday() == TASTE_REFRESH_WEEKDAY:
        run_step("taste_refresh", taste_refresh_cron.main)
    else:
        log_run(logger, "taste_refresh", status="skipped", reason="weekly_not_monday")

    for label, fn in (
        ("journey_pkg", journey_cron.main),
        ("taste_pkg", taste_pkg_cron.main),
        ("pattern_pkg", pattern_cron.main),
        ("stats_pkg", stats_cron.main),
        ("period_pkg", period_cron.main),
    ):
        run_step(label, fn)

    run_step("mood_pkg", mood_cron.main)
    run_step("recap_refresh", recap_cron.main)
    log_run(logger, "match_batch", status="skipped", reason="personal")
    run_step("auto_playlist", auto_playlist_cron.main)
    run_step("account_purge", purge_cron.main)
    run_step("log_cleanup", log_cleanup_cron.main)

    log_run(logger, "worker_nightly", status="done")
    return 0


if __name__ == "__main__":
    sys.exit(main())

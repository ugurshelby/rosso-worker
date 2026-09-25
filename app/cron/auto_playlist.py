"""Otomatik playlist cron — aylık top playlist (opt-in kullanıcılar için).
Çalıştırma: python -m app.cron.auto_playlist.

enabled=true auto_playlist_rules kayıtları için çalışır:
  - top_month: her ay → bir önceki ayın top listesi ("Mayıs - 2026")
  - top_year:  yalnız Ocak → bir önceki yılın top listesi ("2025")
Her platform yeni liste alır. İzole hata yönetimi. Kullanıcı açmadıysa hiçbir şey olmaz.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from app.cron._logging import setup_logging

setup_logging()
logger = logging.getLogger("rosso.worker.cron.auto_playlist")


def main() -> int:
    from app.jobs.auto_playlist_generator import run_auto_playlist_job
    from app.db import get_client
    from app.services import run_log

    client = get_client()

    try:
        result = asyncio.run(run_auto_playlist_job())
        logger.info(
            '{"run":"auto_playlist","status":"%s","rules":%d,"playlists_ok":%d,"errors":%d}',
            result["outcome"], result.get("rules_processed", 0),
            result.get("playlists_ok", 0), result.get("errors", 0),
        )
        run_log.record_run(client, "auto_playlist", outcome=result["outcome"], stats=result)
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("Auto-playlist cron başarısız")
        run_log.record_run(client, "auto_playlist", outcome="error", error=str(exc)[:500])
        return 1


if __name__ == "__main__":
    sys.exit(main())

"""Taste modeli yeniden hesaplama cron — haftalık (§1.9).
Çalıştırma: python -m app.cron.taste_refresh.

play_events'i olan her kullanıcı için refresh_user_taste RPC'sini çağırır
(weights → behavioral → genre_vector → mainstream → identity → materialize).
"""
from __future__ import annotations

import logging
import sys

from app.cron._logging import setup_logging

setup_logging()
logger = logging.getLogger("rosso.worker.cron.taste_refresh")


def main() -> int:
    from app.db import get_client
    from app.pipeline.taste_runner import run_taste_refresh
    from app.services import run_log

    client = get_client()

    try:
        result = run_taste_refresh(client)
        logger.info(
            '{"run":"taste_refresh","status":"%s","users":%d,"errors":%d}',
            result["outcome"], result.get("users_processed", 0), result.get("errors", 0),
        )
        run_log.record_run(client, "taste_refresh", outcome=result["outcome"], stats=result)
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("Taste refresh cron başarısız")
        run_log.record_run(client, "taste_refresh", outcome="error", error=str(exc)[:500])
        return 1


if __name__ == "__main__":
    sys.exit(main())

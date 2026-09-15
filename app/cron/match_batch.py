"""Eşleşme partisi günlük cron — Railway: python -m app.cron.match_batch.

Sosyal profili olan her kullanıcı için build_match_batch RPC'sini çağırır
(üç-kulvar skorlama: Şu An / Değişmeyenler / Nadir Ortak, migration 0079-0083).

Railway cron schedule: 30 4 * * * (her gün 04:30 UTC — taste_refresh'ten
[0 4 * * *] 30 dakika sonra, güncel taste verisiyle skorlama yapılsın diye).
"""
from __future__ import annotations

import logging
import sys

from app.cron._logging import setup_logging

setup_logging()
logger = logging.getLogger("rosso.worker.cron.match_batch")


def main() -> int:
    from app.db import get_client
    from app.pipeline.match_batch_runner import run_match_batch
    from app.services import run_log

    client = get_client()

    try:
        result = run_match_batch(client)
        logger.info(
            '{"run":"match_batch","status":"%s","users":%d,"errors":%d}',
            result["outcome"], result.get("users_processed", 0), result.get("errors", 0),
        )
        run_log.record_run(client, "match_batch", outcome=result["outcome"], stats=result)
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("Match batch cron başarısız")
        run_log.record_run(client, "match_batch", outcome="error", error=str(exc)[:500])
        return 1


if __name__ == "__main__":
    sys.exit(main())

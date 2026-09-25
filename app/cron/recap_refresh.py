"""Recap tazeleme cron'u — v2 (FAZ R3).

Çalıştırma: python -m app.cron.recap_refresh

Donmuş payload modelinin tetiği (plan §4). Kullanıcı recap'i açtığında hesap
yapılmaz; bu cron payload'ı önceden üretir. Kısmi güncelleme sayesinde yeni
kart eklemek eskisini bozmaz.
"""
from __future__ import annotations

import logging
import sys

from app.cron._logging import setup_logging

setup_logging()
logger = logging.getLogger("rosso.worker.cron.recap_refresh")


def main() -> int:
    from app.db import get_client
    from app.pipeline.recap_runner import run_recap_refresh
    from app.services import run_log

    client = get_client()
    try:
        result = run_recap_refresh(client)
        logger.info(
            '{"run":"recap_refresh","status":"%s","users":%d,"written":%d,"errors":%d}',
            result["outcome"], result.get("users_processed", 0),
            result.get("recaps_written", 0), result.get("errors", 0),
        )
        run_log.record_run(
            client, "recap_refresh",
            outcome=result["outcome"],
            stats=result,
            error=result.get("error"),
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("Recap tazeleme başarısız")
        run_log.record_run(
            client, "recap_refresh", outcome="error", error=str(exc)[:500]
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())

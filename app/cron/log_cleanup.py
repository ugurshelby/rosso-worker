"""Operasyonel log temizliği cron'u (Retention Cleanup — migration 0284).

Railway: python -m app.cron.log_cleanup
Sıklık: worker-nightly içinde her gece 1 kez.

Free-tier 500 MB DB kotasını korumak için 30 günden eski başarılı/boş
pipeline_runs ve sistem loglarını temizler. Dinleme geçmişine dokunmaz.
"""
from __future__ import annotations

import logging
import sys

from app.cron._logging import setup_logging

setup_logging()
logger = logging.getLogger("rosso.worker.cron.log_cleanup")


def main() -> int:
    from app.db import get_client
    from app.services import run_log

    client = get_client()
    try:
        res = client.rpc("cleanup_old_logs", {"p_days_to_keep": 30}).execute()
        stats = res.data or {}
        logger.info(
            '{"run":"log_cleanup","status":"success","pipeline_runs_deleted":%d,'
            '"system_logs_deleted":%d,"api_cooldowns_deleted":%d}',
            stats.get("pipeline_runs_deleted", 0),
            stats.get("system_logs_deleted", 0),
            stats.get("api_cooldowns_deleted", 0),
        )
        run_log.record_run(client, "log_cleanup", outcome="success", stats=stats)
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("Log temizliği cron'u başarısız")
        try:
            run_log.record_run(client, "log_cleanup", outcome="error", error=str(exc)[:500])
        except Exception:  # noqa: BLE001
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())

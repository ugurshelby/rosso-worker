"""Otomatik purge cron entry — soft-delete süresi (30 gün) dolan hesapları
kalıcı siler, çıkar. Railway: python -m app.cron.account_purge.

⚠ YIKICI cron (auth.users CASCADE). Ama yalnız 30 günü GERÇEKTEN dolmuş,
soft-delete edilmiş hesaplara dokunur (grace penceresi + kurtarma guard'ı
zaten geçmiş). Dış API'ye istek atmaz (§1.6 konusu yok). Günde 1 kez yeter —
silme aciliyeti yok, 30 gün zaten geçti.
"""
from __future__ import annotations

import logging
import sys

from app.cron._logging import setup_logging

setup_logging()
logger = logging.getLogger("rosso.worker.cron.account_purge")


def main() -> int:
    from app.db import get_client
    from app.pipeline.account_purge_runner import run_account_purge
    from app.services import run_log

    client = get_client()
    try:
        result = run_account_purge(client, batch_limit=25)
        logger.info(
            '{"run":"account_purge","status":"%s","eligible":%d,"purged":%d,"failed":%d,"storage_files":%d}',
            result["outcome"], result.get("eligible", 0), result.get("purged", 0),
            result.get("failed", 0), result.get("storage_files", 0),
        )
        run_log.record_run(client, "account_purge", outcome=result["outcome"], stats=result)
        return 0 if result["outcome"] != "error" else 1
    except Exception as exc:  # noqa: BLE001
        logger.exception("Otomatik purge başarısız")
        run_log.record_run(client, "account_purge", outcome="error", error=str(exc)[:500])
        return 1


if __name__ == "__main__":
    sys.exit(main())

"""Saatlik dağılım + Platform donut paketi cron'u (Aşama 3 · paket #3).

Çalıştırma: python -m app.cron.pattern_pkg  ·  GÜNLÜK

`user_pattern_pkg` tablosunu doldurur. Dashboard `insights-row` bileşenini
besler (saatlik bar chart + zirve saat + platform donut).

⚠ `taste_pkg` ile KARIŞTIRMA — ikisi farklı paket:
    taste_pkg   → user_taste_pkg:   tür dağılımı, kronotip saat kutuları (/taste)
    pattern_pkg → user_pattern_pkg: saatlik dağılım + platform donut (/dashboard)

Runner tazelik kontrolü yapıyor (20 saat), yani günlük tetiklemek güvenli:
aynı gün ikinci kez koşarsa hiçbir kullanıcı yeniden hesaplanmaz.
"""
from __future__ import annotations

import logging
import sys

from app.cron._logging import setup_logging

setup_logging()
logger = logging.getLogger("rosso.worker.cron.pattern_pkg")


def main() -> int:
    from app.db import get_client
    from app.pipeline.pattern_pkg_runner import run_pattern_pkg
    from app.services import run_log

    client = get_client()
    try:
        result = run_pattern_pkg(client)
        logger.info(
            '{"run":"pattern_pkg","status":"%s","users":%d,"skipped":%d,"empty":%d,"errors":%d}',
            result["outcome"],
            result.get("users_processed", 0),
            result.get("skipped", 0),
            result.get("empty", 0),
            result.get("errors", 0),
        )
        run_log.record_run(client, "pattern_pkg", outcome=result["outcome"], stats=result)
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("Pattern paket cron'u başarısız")
        run_log.record_run(client, "pattern_pkg", outcome="error", error=str(exc)[:500])
        return 1


if __name__ == "__main__":
    sys.exit(main())

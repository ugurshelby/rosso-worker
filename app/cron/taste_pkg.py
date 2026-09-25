"""Tür DNA + Kronotip paketi cron'u (Aşama 3 · paket #2).

Çalıştırma: python -m app.cron.taste_pkg  ·  GÜNLÜK

`user_taste_pkg` tablosunu doldurur. Üç yüzeyi besler: /taste (birincil),
/profile ve /u/[username] (yan bölüm).

⚠ `taste_refresh` ile KARIŞTIRMA — ikisi farklı iş:
    taste_refresh (HAFTALIK) → user_taste_profile: kimlik rozetleri, şeritler
    taste_pkg     (GÜNLÜK)   → user_taste_pkg: tür dağılımı, saat kutuları

Runner tazelik kontrolü yapıyor (20 saat), yani günlük tetiklemek güvenli:
aynı gün ikinci kez koşarsa hiçbir kullanıcı yeniden hesaplanmaz.
"""
from __future__ import annotations

import logging
import sys

from app.cron._logging import setup_logging

setup_logging()
logger = logging.getLogger("rosso.worker.cron.taste_pkg")


def main() -> int:
    from app.db import get_client
    from app.pipeline.taste_pkg_runner import run_taste_pkg
    from app.services import run_log

    client = get_client()
    try:
        result = run_taste_pkg(client)
        logger.info(
            '{"run":"taste_pkg","status":"%s","users":%d,"skipped":%d,"empty":%d,"errors":%d}',
            result["outcome"],
            result.get("users_processed", 0),
            result.get("skipped", 0),
            result.get("empty", 0),
            result.get("errors", 0),
        )
        run_log.record_run(client, "taste_pkg", outcome=result["outcome"], stats=result)
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("Taste paket cron'u başarısız")
        run_log.record_run(client, "taste_pkg", outcome="error", error=str(exc)[:500])
        return 1


if __name__ == "__main__":
    sys.exit(main())

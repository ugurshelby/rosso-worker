"""Journey yıl paketi cron'u (Aşama 3 · paket #1).

Railway: python -m app.cron.journey_pkg

`journey_year_pkg` tablosunu doldurur. `/journey` yalnız bu paketi okur
(0,13 ms); paket yoksa "hazırlanıyor" gösterir — canlı hesaba ASLA düşmez.

ZAMANLAMA: Günlük koşabilir ama işi AYLIK yapar — runner her kullanıcı için
paketin 30 günden eski olup olmadığına bakar, tazeyse atlar (Efendim'in
tazelik kararı, 2026-07-31). Yani günlük tetiklemek maliyetsizdir; gerçek
üretim ayda bir kez olur.

Neden günlük tetikleniyor: yeni kullanıcı (ilk üretim) ve faz geçişi
sonrası paket hazırlığı gecikmesin. Aylık tetiklenseydi Faz 3'e geçen
kullanıcı en kötü ihtimalle 30 gün "hazırlanıyor" görürdü.
"""
from __future__ import annotations

import logging
import sys

from app.cron._logging import setup_logging

setup_logging()
logger = logging.getLogger("rosso.worker.cron.journey_pkg")


def main() -> int:
    from app.db import get_client
    from app.pipeline.journey_pkg_runner import run_journey_pkg
    from app.services import run_log

    client = get_client()
    try:
        result = run_journey_pkg(client)
        logger.info(
            '{"run":"journey_pkg","status":"%s","users":%d,"years":%d,"skipped":%d,"errors":%d}',
            result["outcome"],
            result.get("users_processed", 0),
            result.get("years_written", 0),
            result.get("skipped", 0),
            result.get("errors", 0),
        )
        run_log.record_run(
            client, "journey_pkg",
            outcome=result["outcome"],
            stats=result,
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("Journey paket cron'u başarısız")
        run_log.record_run(client, "journey_pkg", outcome="error", error=str(exc)[:500])
        return 1


if __name__ == "__main__":
    sys.exit(main())

"""Dashboard "Dinlemelerim" tüm-zamanlar paketi cron'u (Aşama 3 · paket #5).

Çalıştırma: python -m app.cron.period_pkg  ·  GÜNLÜK

`user_period_pkg` tablosunu doldurur: özet sayımlar + top şarkılar +
top sanatçılar (yalnız TÜM ZAMANLAR penceresi).

⚠ Diğer paket cron'larıyla KARIŞTIRMA — beşi ayrı iş:
    taste_pkg   → user_taste_pkg:   tür dağılımı + kronotip        (/taste)
    pattern_pkg → user_pattern_pkg: saatlik dağılım + platform     (/dashboard)
    stats_pkg   → user_stats_pkg:   StatBar sayımları + tür haritası
    period_pkg  → user_period_pkg:  Dinlemelerim top listeleri     (/dashboard)
    journey_pkg → journey_year_pkg: yıl kartları + kapaklar        (/journey)

⚠ `stats_pkg` ile `period_pkg` ikisi de "sayım" üretiyor ama FARKLI soruya
cevap veriyor:
    stats_pkg   → KAÇ farklı şarkı/sanatçı/tür (StatBar'ın dört kutusu)
    period_pkg  → HANGİ şarkılar/sanatçılar (top listeleri)
Aynı cron'a konsalardı biri diğerinin tazelik ihtiyacını taşımak zorunda kalırdı.

Runner tazelik kontrolü yapıyor (20 saat), yani günlük tetiklemek güvenli:
aynı gün ikinci kez koşarsa hiçbir kullanıcı yeniden hesaplanmaz.
"""
from __future__ import annotations

import logging
import sys

from app.cron._logging import setup_logging

setup_logging()
logger = logging.getLogger("rosso.worker.cron.period_pkg")


def main() -> int:
    from app.db import get_client
    from app.pipeline.period_pkg_runner import run_period_pkg
    from app.services import run_log

    client = get_client()
    try:
        result = run_period_pkg(client)
        logger.info(
            '{"run":"period_pkg","status":"%s","users":%d,"skipped":%d,"empty":%d,"errors":%d}',
            result["outcome"],
            result.get("users_processed", 0),
            result.get("skipped", 0),
            result.get("empty", 0),
            result.get("errors", 0),
        )
        run_log.record_run(client, "period_pkg", outcome=result["outcome"], stats=result)
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("Period paket cron'u başarısız")
        run_log.record_run(client, "period_pkg", outcome="error", error=str(exc)[:500])
        return 1


if __name__ == "__main__":
    sys.exit(main())

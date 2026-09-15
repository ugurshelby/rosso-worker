"""Dashboard StatBar + Profil tür haritası paketi cron'u (Aşama 3 · paket #4).

Railway: python -m app.cron.stats_pkg  ·  GÜNLÜK

`user_stats_pkg` tablosunu doldurur. Dashboard StatBar'ın dört kutusunu
(şarkı / sanatçı / tür / dinleme) ve Profil tür haritasını besler.

⚠ Diğer paket cron'larıyla KARIŞTIRMA — dördü ayrı iş:
    taste_pkg   → user_taste_pkg:   tür dağılımı + kronotip        (/taste)
    pattern_pkg → user_pattern_pkg: saatlik dağılım + platform     (/dashboard)
    stats_pkg   → user_stats_pkg:   StatBar sayımları + tür haritası
                                    (/dashboard + /profile)
    journey_pkg → journey_year_pkg: yıl kartları + kapaklar        (/journey)

⚠ `taste_pkg` ile `stats_pkg` ikisi de "tür" üretiyor ama FARKLI soruya cevap:
    taste_pkg  → tür DAĞILIMI (yüzdeler, kimlik rozetleri)
    stats_pkg  → tür SAYISI (StatBar'ın dördüncü kutusu) + Profil haritası
Aynı cron'a konsalardı biri diğerinin tazelik ihtiyacını taşımak zorunda kalırdı.

Runner tazelik kontrolü yapıyor (20 saat), yani günlük tetiklemek güvenli:
aynı gün ikinci kez koşarsa hiçbir kullanıcı yeniden hesaplanmaz.
"""
from __future__ import annotations

import logging
import sys

from app.cron._logging import setup_logging

setup_logging()
logger = logging.getLogger("rosso.worker.cron.stats_pkg")


def main() -> int:
    from app.db import get_client
    from app.pipeline.stats_pkg_runner import run_stats_pkg
    from app.services import run_log

    client = get_client()
    try:
        result = run_stats_pkg(client)
        logger.info(
            '{"run":"stats_pkg","status":"%s","users":%d,"skipped":%d,"empty":%d,"errors":%d}',
            result["outcome"],
            result.get("users_processed", 0),
            result.get("skipped", 0),
            result.get("empty", 0),
            result.get("errors", 0),
        )
        run_log.record_run(client, "stats_pkg", outcome=result["outcome"], stats=result)
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("Stats paket cron'u başarısız")
        run_log.record_run(client, "stats_pkg", outcome="error", error=str(exc)[:500])
        return 1


if __name__ == "__main__":
    sys.exit(main())

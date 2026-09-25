"""Sanatçı görseli ön-doldurma cron entry — boş artists.image_url'e Spotify görseli.
Çalıştırma: python -m app.cron.artist_image_backfill.

⚠ Bu cron gerçek Spotify istekleri atar (§1.6) — sanatçı başına 2 istek (/tracks +
/artists). Efendim'in onayı/gözetimiyle çalıştırılır — küçük batch +
zaman bütçesi ile turlar hâlinde ilerler, tek gecede binlerce istek atmaz. Ortak
"spotify" cooldown havuzunu cover_backfill + genre fallback + katalog ile paylaşır.
"""
from __future__ import annotations

import logging
import sys

from app.cron._logging import log_run, setup_logging

setup_logging()
logger = logging.getLogger("rosso.worker.cron.artist_image_backfill")


def main() -> int:
    import httpx

    from app.config import get_settings
    from app.db import get_client
    from app.cron._gruplu import gruplu_calistir
    from app.pipeline.artist_image_backfill_runner import run_artist_image_backfill
    from app.services import run_log

    client = get_client()
    settings = get_settings()
    try:
        with httpx.Client(timeout=15) as http:
            # Her kullanıcı KENDİ Spotify app'inin kotasıyla (2026-09-23).
            result = gruplu_calistir(
                client, settings,
                lambda grup, kalan: run_artist_image_backfill(
                    client, settings, http, batch_limit=60,
                    time_budget_s=min(kalan, 200.0), grup=grup,
                ),
                toplam_butce_s=200.0,
            )
        # ⚠ `quota_hit` bir BOOL — bkz. cover_backfill'deki aynı düzeltme.
        log_run(
            logger, "artist_image_backfill",
            status=result["outcome"],
            processed=result.get("processed", 0),
            updated=result.get("updated", 0),
            skipped=result.get("skipped", 0),
            quota_hit=result.get("quota_hit", False),
        )
        run_log.record_run(client, "artist_image_backfill", outcome=result["outcome"], stats=result)
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("Sanatçı görseli dolgusu başarısız")
        run_log.record_run(client, "artist_image_backfill", outcome="error", error=str(exc)[:500])
        return 1


if __name__ == "__main__":
    sys.exit(main())

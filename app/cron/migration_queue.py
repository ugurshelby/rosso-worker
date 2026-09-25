"""Taşıma kuyruğu cron'u — sıradaki partileri işler.

Çalıştırma: python -m app.cron.migration_queue

MİMARİ NOTU (önemli): bu cron **iş yapmaz**, yalnızca ZAMANLAYICIDIR.
Taşıma motoru (Spotify/Apple/YT istemcileri, token yönetimi, eşleşme önbelleği)
Next.js'te yaşıyor. Python'a kopyalamak İKİ KOD üretirdi — biri düzeltilir, öteki
eskir; sapmanın klasik kaynağı. Bu yüzden cron, Next'in korumalı
`/api/internal/migration-queue` ucunu çağırır.

Ters yön zaten var: Next → worker `/internal/refresh` (FAZ 2). Aynı paylaşılan
sır, aynı desen.

Kapasite dolduğunda uç `{"idle": true, "reason": "günlük kapasite doldu"}` döner.
Bu HATA DEĞİLDİR → outcome 'empty'. Zamanlayıcıda exit≠0 alarm üretir; "bugünlük
kota bitti" alarm değildir.
"""
from __future__ import annotations

import logging
import os
import sys

import httpx

from app.cron._logging import setup_logging

setup_logging()
logger = logging.getLogger("rosso.worker.cron.migration_queue")

# Bir turda en fazla kaç parti işlenir. Kapasite zaten sunucu tarafında
# sınırlı; bu yalnızca cron'un sonsuza kadar dönmesini engeller.
_MAX_BATCHES = 5


def _app_url() -> str:
    return (
        os.environ.get("NEXT_PUBLIC_APP_URL")
        or os.environ.get("APP_URL")
        or "http://localhost:3847"
    ).rstrip("/")


def main() -> int:
    from app.db import get_client
    from app.services import run_log

    client = get_client()
    secret = os.environ.get("WORKER_SHARED_SECRET", "")

    if not secret:
        # Sır yoksa uç 401 döner — sessizce "başarılı" demek YALAN olur.
        logger.error("WORKER_SHARED_SECRET tanımsız — kuyruk işlenemez")
        run_log.record_run(
            client, "migration_queue", outcome="error",
            error="WORKER_SHARED_SECRET tanımsız",
        )
        return 1

    url = f"{_app_url()}/api/internal/migration-queue"
    processed = 0
    added = 0
    batches = 0

    try:
        with httpx.Client(timeout=120) as http:
            for _ in range(_MAX_BATCHES):
                resp = http.post(url, headers={"X-Worker-Secret": secret})
                resp.raise_for_status()
                data = resp.json()

                # Kuyruk boş ya da kapasite doldu → dur. Hata DEĞİL.
                if data.get("idle"):
                    logger.info(
                        '{"run":"migration_queue","idle":true,"reason":"%s"}',
                        data.get("reason", ""),
                    )
                    break

                batches += 1
                processed += int(data.get("processed", 0) or 0)
                added += int(data.get("added", 0) or 0)

                # İş bitti → sıradaki işe geçmek için döngü devam eder.
                if data.get("status") == "failed":
                    break

        outcome = "success" if batches else "empty"
        stats = {"batches": batches, "processed": processed, "added": added}
        logger.info(
            '{"run":"migration_queue","status":"%s","batches":%d,"processed":%d,"added":%d}',
            outcome, batches, processed, added,
        )
        run_log.record_run(client, "migration_queue", outcome=outcome, stats=stats)
        return 0

    except Exception as exc:  # noqa: BLE001
        logger.exception("Taşıma kuyruğu işlenemedi")
        run_log.record_run(
            client, "migration_queue", outcome="error", error=str(exc)[:500]
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())

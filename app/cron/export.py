"""Export cron entry — bekleyen 1 ZIP'i işle, çık. Çalıştırma: python -m app.cron.export."""
from __future__ import annotations

import logging
import sys
from typing import Any

from app.cron._logging import log_run, setup_logging

setup_logging()
logger = logging.getLogger("rosso.worker.cron.export")


def recover_stale_jobs(client, *, stale_minutes: int = 15) -> None:
    """Çökerek 'processing'de takılı kalan işleri 'queued'a geri al.

    Bir cron çalışması iş yaparken SIGKILL yerse (ör. eski timeout bug'ı) job
    'processing'de asılı kalır ve bir daha hiç işlenmez (cron yalnızca 'queued'
    seçer). Bu fonksiyon başlangıçta eski 'processing' işleri kuyruğa iade eder.

    stale_minutes: lookup'sız export saniyeler sürdüğü için 15 dk fazlasıyla güvenli.
    """
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)).isoformat()
    try:
        res = client.table("export_jobs").update({"status": "queued"}).eq(
            "status", "processing"
        ).lt("started_at", cutoff).execute()
        # B9: eskiden bu log koşulsuzdu → 15 saatte 182 yanıltıcı "recovery" satırı
        # (hiçbir iş kurtarılmamışken). Gerçek bir kurtarma olduğunda ayırt edemiyorduk.
        # Artık yalnız fiilen satır güncellendiyse konuşuyoruz.
        recovered = len(res.data or [])
        if recovered:
            logger.info(
                '{"run":"export","status":"recovery","recovered":%s,'
                '"msg":"stale processing -> queued"}' % recovered
            )
    except Exception:  # noqa: BLE001
        logger.warning("stale-job recovery başarısız")


def expire_abandoned_uploads(client, *, stale_hours: int = 3) -> int:
    """Yükleme yarım kalmış ('uploading' > stale_hours) işleri kapat ve dosyalarını sil.

    Kullanıcı sekmeyi kapatırsa ya da bağlantı koparsa `/api/export/queue`
    hiç çağrılmaz; iş 'uploading'de asılı kalır ve Storage'da yarım/tam bir
    dosya durur. Web aktif-iş sınırı bu işleri 3 saat sonra saymaz, ama depoyu
    temizleyen bu fonksiyon. Hata turu bozmaz.
    """
    from datetime import datetime, timedelta, timezone

    from app.config import get_settings

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=stale_hours)).isoformat()
    try:
        res = (
            client.table("export_jobs")
            .select("id, file_path")
            .eq("status", "uploading")
            .lt("created_at", cutoff)
            .limit(200)
            .execute()
        )
        rows = res.data or []
        if not rows:
            return 0
        bucket = get_settings().export_bucket
        yollar = [r["file_path"] for r in rows if r.get("file_path")]
        if yollar:
            try:
                client.storage.from_(bucket).remove(yollar)
            except Exception:  # noqa: BLE001
                logger.warning("terk edilmiş yüklemelerin dosyaları silinemedi")
        client.table("export_jobs").update({
            "status": "failed",
            "error_message": "yukleme_tamamlanmadi",
        }).in_("id", [r["id"] for r in rows]).eq("status", "uploading").execute()
        return len(rows)
    except Exception:  # noqa: BLE001
        logger.warning("terk edilmiş yükleme temizliği başarısız")
        return 0


def process_next_export_job(client: Any, settings: Any) -> dict[str, Any]:
    """Kuyruktan bir export işle. {outcome, job_id?, has_more_queued} döner."""
    from datetime import datetime, timezone

    from app.pipeline.export_runner import run_one_export
    from app.services import run_log

    # ATOMİK SAHİPLENME (2026-09-23, çok kullanıcı denetimi). Her ZIP yüklemesi
    # ayrı bir GitHub Actions çalışması uyandırır; iki kullanıcı aynı anda
    # yüklerse iki çalışma AYNI 'queued' işi seçebiliyordu (seç → id ile
    # güncelle, arada kilit yok) ve ZIP iki kez işleniyordu. Güncelleme artık
    # `status='queued'` koşuluyla yapılır: satırı yalnız bir çalışma çevirebilir,
    # kaybeden bir sonraki işe geçer.
    job: dict[str, Any] | None = None
    for _ in range(5):
        res = (
            client.table("export_jobs")
            .select("id, user_id, file_path, status")
            .eq("status", "queued")
            .order("created_at")
            .limit(1)
            .execute()
        )
        jobs = res.data or []
        if not jobs:
            return {"outcome": "empty", "job_id": None, "has_more_queued": False}

        claim = (
            client.table("export_jobs")
            .update({
                "status": "processing",
                "started_at": datetime.now(timezone.utc).isoformat(),
            })
            .eq("id", jobs[0]["id"])
            .eq("status", "queued")
            .execute()
        )
        if claim.data:
            job = jobs[0]
            break
    if job is None:
        return {"outcome": "empty", "job_id": None, "has_more_queued": _has_queued_exports(client)}

    job_id = job["id"]

    try:
        result = run_one_export(client, settings, job)
        if result["outcome"] == "success":
            events = result.get("events", 0)
            matched = result.get("matched_events", events)
            total = events if events > 0 else matched
            _update: dict = {
                "status": "completed",
                "genre_pending": bool(result.get("tracks", 0)),
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "total_events": total,
                "processed_events": total,
                "matched_events": matched,
                "skipped_events": result.get("skipped_events", 0),
            }
            if result.get("zip_type"):
                _update["export_type"] = result["zip_type"]
            if result.get("period_start"):
                _update["period_start"] = result["period_start"]
                _update["period_end"] = result["period_end"]
            client.table("export_jobs").update(_update).eq("id", job_id).execute()
            logger.info(
                '{"run":"export","status":"done","job":"%s","type":"%s","tracks":%d,"events":%d,'
                '"matched":%d,"skipped":%d,"elapsed_ms":%d}',
                job_id, result.get("zip_type", "?"), result.get("tracks", 0),
                events, matched, result.get("skipped_events", 0),
                result.get("elapsed_ms", 0),
            )
            run_log.record_run(client, "export", job_id=job_id, outcome="success", stats=result)

            try:
                from app.pipeline.post_import_refresh import refresh_after_import

                refresh_after_import(client, job["user_id"])
            except Exception:  # noqa: BLE001
                logger.warning("ZIP-sonrası tazeleme atlandı (export başarılı, cron toparlar)")
        else:
            _fail: dict = {
                "status": "failed",
                "error_message": str(result.get("error", "unknown"))[:500],
            }
            if result.get("zip_type"):
                _fail["export_type"] = result["zip_type"]
            client.table("export_jobs").update(_fail).eq("id", job_id).execute()
            run_log.record_run(
                client, "export", job_id=job_id, outcome="error",
                error=str(result.get("error")),
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Export başarısız: job=%s", job_id)
        client.table("export_jobs").update({
            "status": "failed", "error_message": str(exc)[:500],
        }).eq("id", job_id).execute()
        run_log.record_run(client, "export", job_id=job_id, outcome="error", error=str(exc)[:500])
        return {"outcome": "error", "job_id": job_id, "has_more_queued": _has_queued_exports(client)}

    return {
        "outcome": result.get("outcome", "success"),
        "job_id": job_id,
        "has_more_queued": _has_queued_exports(client),
    }


def _has_queued_exports(client: Any) -> bool:
    try:
        res = (
            client.table("export_jobs")
            .select("id")
            .eq("status", "queued")
            .limit(1)
            .execute()
        )
        return bool(res.data)
    except Exception:  # noqa: BLE001
        return False


def run_export_burst(*, time_budget_s: float = 300.0) -> dict[str, Any]:
    """Bekleyen export'ları zaman bütçesi içinde sırayla işle (worker-fast)."""
    import time

    from app.config import get_settings
    from app.db import get_client
    from app.services import run_log

    client = get_client()
    settings = get_settings()
    recover_stale_jobs(client)
    expire_abandoned_uploads(client)

    started = time.monotonic()
    processed = 0
    last_outcome = "empty"

    while True:
        elapsed = time.monotonic() - started
        if processed > 0 and elapsed >= time_budget_s:
            break

        step = process_next_export_job(client, settings)
        last_outcome = str(step.get("outcome", "empty"))
        if last_outcome == "empty":
            break
        processed += 1
        if not step.get("has_more_queued"):
            break

    if processed == 0:
        log_run(logger, "export", status="empty", msg="bekleyen is yok")
        run_log.record_run(client, "export", outcome="empty")
    else:
        log_run(
            logger, "export_burst",
            status="done",
            processed=processed,
            elapsed_s=round(time.monotonic() - started, 2),
            last_outcome=last_outcome,
        )

    return {"processed": processed, "last_outcome": last_outcome}


def main() -> int:
    from app.config import get_settings
    from app.db import get_client
    from app.services import run_log

    client = get_client()
    settings = get_settings()
    recover_stale_jobs(client)

    step = process_next_export_job(client, settings)
    if step["outcome"] == "empty":
        log_run(logger, "export", status="empty", msg="bekleyen is yok")
        run_log.record_run(client, "export", outcome="empty")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())

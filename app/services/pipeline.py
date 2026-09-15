"""Pipeline monitoring helper — export_jobs.pipeline_step + pipeline_events.

Her adım başında ve sonunda pipeline_step_update RPC'si çağrılır.
Fire-and-forget: log yazma başarısız olsa bile worker operasyonu etkilenmez.
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator

_LOG = logging.getLogger("rosso.pipeline")


def step_start(
    client: Any,
    job_id: str,
    user_id: str,
    step: str,
) -> datetime:
    """Adım başladı — pipeline_step 'started' olarak işaretle. Başlangıç zamanını döndür."""
    started_at = datetime.now(timezone.utc)
    try:
        client.rpc("pipeline_step_update", {
            "p_job_id":     job_id,
            "p_user_id":    user_id,
            "p_step":       step,
            "p_status":     "started",
            "p_started_at": started_at.isoformat(),
        }).execute()
        _LOG.info("pipeline [%s] started (job=%s)", step, job_id)
    except Exception:
        _LOG.warning("pipeline_step_update başarısız (step=%s started) — devam", step)
    return started_at


def step_complete(
    client: Any,
    job_id: str,
    user_id: str,
    step: str,
    started_at: datetime,
    counts: dict[str, int] | None = None,
) -> None:
    """Adım tamamlandı — süre ve sayıları yaz."""
    try:
        client.rpc("pipeline_step_update", {
            "p_job_id":     job_id,
            "p_user_id":    user_id,
            "p_step":       step,
            "p_status":     "completed",
            "p_started_at": started_at.isoformat(),
            "p_counts":     counts,
        }).execute()
        duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
        _LOG.info("pipeline [%s] completed in %dms counts=%s (job=%s)", step, duration_ms, counts, job_id)
    except Exception:
        _LOG.warning("pipeline_step_update başarısız (step=%s completed) — devam", step)


def step_fail(
    client: Any,
    job_id: str,
    user_id: str,
    step: str,
    started_at: datetime,
    error: str,
) -> None:
    """Adım başarısız — hata mesajını yaz."""
    try:
        client.rpc("pipeline_step_update", {
            "p_job_id":     job_id,
            "p_user_id":    user_id,
            "p_step":       step,
            "p_status":     "failed",
            "p_started_at": started_at.isoformat(),
            "p_error":      error[:500],
        }).execute()
        _LOG.warning("pipeline [%s] failed: %s (job=%s)", step, error[:200], job_id)
    except Exception:
        _LOG.warning("pipeline_step_update başarısız (step=%s failed) — devam", step)


@contextmanager
def tracked_step(
    client: Any,
    job_id: str,
    user_id: str,
    step: str,
) -> Generator[dict[str, Any], None, None]:
    """Context manager: adım başında started, çıkışta completed/failed yazar.

    Kullanım:
        with tracked_step(client, job_id, user_id, "inserting_tracks") as ctx:
            n = do_work()
            ctx["counts"] = {"inserted": n}
    """
    started_at = step_start(client, job_id, user_id, step)
    ctx: dict[str, Any] = {"counts": None}
    try:
        yield ctx
        step_complete(client, job_id, user_id, step, started_at, ctx.get("counts"))
    except Exception as exc:
        step_fail(client, job_id, user_id, step, started_at, str(exc))
        raise

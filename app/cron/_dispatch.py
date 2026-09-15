"""Ortak cron dispatcher yardımcıları (worker-fast / worker-nightly)."""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("rosso.worker.cron.dispatch")


def last_success_at(client: Any, run_type: str) -> datetime | None:
    """`pipeline_runs` içinde bu run_type için son başarılı tur zamanı."""
    try:
        res = (
            client.table("pipeline_runs")
            .select("created_at")
            .eq("run_type", run_type)
            .eq("outcome", "success")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            return None
        raw = rows[0].get("created_at")
        if not raw:
            return None
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        logger.warning("last_success_at sorgusu başarısız run_type=%s", run_type)
        return None


def hours_since_last_success(client: Any, run_type: str) -> float | None:
    """Son başarılı turdan bu yana geçen saat. Hiç kayıt yoksa None."""
    last = last_success_at(client, run_type)
    if last is None:
        return None
    delta = datetime.now(timezone.utc) - last.astimezone(timezone.utc)
    return delta.total_seconds() / 3600.0


def should_run_interval(
    client: Any,
    run_type: str,
    *,
    min_hours: float,
) -> bool:
    """Son başarılı tur `min_hours` saatten eskiyse (veya hiç yoksa) True."""
    hours = hours_since_last_success(client, run_type)
    if hours is None:
        return True
    return hours >= min_hours


def run_step(label: str, fn: Callable[[], int]) -> int:
    """Bir alt cron'u izole çalıştır — hata diğer adımları durdurmaz."""
    try:
        code = fn()
        logger.info('{"dispatcher_step":"%s","exit_code":%d}', label, code)
        return code
    except Exception:  # noqa: BLE001
        logger.exception('{"dispatcher_step":"%s","exit_code":1,"error":"unhandled"}', label)
        return 1

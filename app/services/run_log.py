"""pipeline_runs'a tek satır kayıt — stdout şişmeden DB'de detay tutulur."""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("rosso.worker.run_log")


def record_run(
    client: Any,
    run_type: str,
    *,
    job_id: str | None = None,
    outcome: str,
    stats: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """Bir cron çalıştırmasının sonucunu pipeline_runs'a yaz. Hata yutulur."""
    try:
        client.rpc(
            "record_run",
            {
                "p_run_type": run_type,
                "p_job_id": job_id,
                "p_outcome": outcome,
                "p_stats": stats or {},
                "p_error": error,
            },
        ).execute()
    except Exception:  # noqa: BLE001
        logger.warning("record_run başarısız (run_type=%s) — ana işi etkilemez", run_type)

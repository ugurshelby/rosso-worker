"""Rosso worker sistem loggeri — system_logs tablosuna service_role ile yazar.

Fire-and-forget: log yazma başarısız olsa bile worker operasyonu etkilenmez.
ip_addr asla yazılmaz (CLAUDE.md kuralı).
"""
from __future__ import annotations

import logging
from typing import Any

_LOG = logging.getLogger("rosso.system_logger")

SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"
SEVERITY_ERROR = "error"
SEVERITY_CRITICAL = "critical"


def log_event(
    *,
    operation: str,
    severity: str = SEVERITY_INFO,
    user_id: str | None = None,
    platform: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    related_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """system_logs tablosuna bir satır yaz.

    Hata durumunda sessizce loglar, exception fırlatmaz (fire-and-forget).
    """
    try:
        from app.db import get_client  # geç import — DB olmasa bile import edilebilir
        import os
        if "PYTEST_CURRENT_TEST" in os.environ and not hasattr(get_client, "assert_called"):
            # Test koşusu .env.local'i yükler — bu guard olmasa runner testleri
            # PRODUCTION system_logs'a satır basardı. get_client mock'lanmışsa
            # (system_logger birim testleri) yazma akışı normal test edilir.
            return
        client = get_client()
        client.table("system_logs").insert(
            {
                "user_id": user_id,
                "operation": operation,
                "platform": platform,
                "error_code": error_code,
                "error_message": error_message[:2000] if error_message else None,
                "severity": severity,
                "related_id": related_id,
                "metadata": metadata,
            }
        ).execute()
    except Exception as exc:  # noqa: BLE001
        # B12: eskiden burası yalnız "DB yazma başarısız" derdi — nedeni yutulduğu için
        # araştırılamıyordu (log sisteminin kendisi sessiz-başarısızlık deseni taşıyordu,
        # B19'un ta kendisi). Artık hatayı ve yazmaya çalıştığımız operasyonu söylüyoruz.
        # Fire-and-forget kalıyor (log yazımı worker'ı çöktürmez) ama artık kör değiliz.
        _LOG.warning(
            "system_logger: DB yazma başarısız (operation=%s severity=%s): %s: %s",
            operation,
            severity,
            type(exc).__name__,
            exc,
            exc_info=True,
        )

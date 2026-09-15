"""JSON log formatter — extra alanlarının artık payload'a düştüğünü doğrular.

Canlı kanıt (2026-07-13): ytmusic_official.py'deki logger.error(..., extra={...})
çağrıları formatter tarafından SESSİZCE yutuluyordu — yalnız level/logger/msg
yazılıyordu. Bu, bir hatanın gerçek nedenini (reason/status/body) hiçbir zaman
Railway loglarında göremememizin kök nedeniydi.
"""
import json
import logging

from app.cron._logging import _JSONFormatter


def test_formatter_includes_extra_fields():
    formatter = _JSONFormatter()
    record = logging.LogRecord(
        name="rosso.worker.test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="something_failed",
        args=(),
        exc_info=None,
    )
    record.reason = "SERVICE_UNAVAILABLE"
    record.http_status = 409

    payload = json.loads(formatter.format(record))

    assert payload["reason"] == "SERVICE_UNAVAILABLE"
    assert payload["http_status"] == 409
    assert payload["level"] == "ERROR"
    assert payload["msg"] == "something_failed"


def test_formatter_still_works_without_extra_fields():
    formatter = _JSONFormatter()
    record = logging.LogRecord(
        name="rosso.worker.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="plain message",
        args=(),
        exc_info=None,
    )
    payload = json.loads(formatter.format(record))
    assert payload == {"level": "INFO", "logger": "rosso.worker.test", "msg": "plain message"}

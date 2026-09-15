"""system_logger izolasyon testleri."""
from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_log_event_does_not_raise_on_db_failure() -> None:
    """DB insert başarısız olsa bile exception fırlatmamalı."""
    with patch("app.db.get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.table.return_value.insert.return_value.execute.side_effect = (
            RuntimeError("DB bağlantı hatası")
        )
        mock_get_client.return_value = mock_client

        from app.services.logger import log_event
        log_event(operation="test_op", severity="error", error_code="TEST_ERROR")


def test_log_event_writes_correct_fields() -> None:
    """Doğru alanları yazar, ip_addr yazmaz."""
    with patch("app.db.get_client") as mock_get_client:
        mock_client = MagicMock()
        insert_mock = MagicMock()
        mock_client.table.return_value.insert = insert_mock
        insert_mock.return_value.execute.return_value = None
        mock_get_client.return_value = mock_client

        from app.services.logger import log_event
        log_event(
            operation="export_upload",
            severity="error",
            user_id="user-123",
            error_code="EXPORT_UPLOAD_FAILED",
            error_message="Zip parse failed",
            related_id="job-456",
        )

        call_args = insert_mock.call_args[0][0]
        assert call_args["operation"] == "export_upload"
        assert call_args["error_code"] == "EXPORT_UPLOAD_FAILED"
        assert call_args["user_id"] == "user-123"
        assert "ip_addr" not in call_args


def test_log_event_truncates_long_error_message() -> None:
    """2000 karakterden uzun hata mesajı kesilmeli."""
    with patch("app.db.get_client") as mock_get_client:
        mock_client = MagicMock()
        insert_mock = MagicMock()
        mock_client.table.return_value.insert = insert_mock
        insert_mock.return_value.execute.return_value = None
        mock_get_client.return_value = mock_client

        from app.services.logger import log_event
        long_msg = "x" * 3000
        log_event(operation="test", error_message=long_msg)

        call_args = insert_mock.call_args[0][0]
        assert len(call_args["error_message"]) == 2000

"""Dispatcher yardımcıları testleri."""
from datetime import datetime, timedelta, timezone

from app.cron._dispatch import hours_since_last_success, should_run_interval


class _Q:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        return type("R", (), {"data": self._rows})()


class _Client:
    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        assert name == "pipeline_runs"
        return _Q(self._rows)


def test_should_run_interval_no_prior_run():
    client = _Client([])
    assert should_run_interval(client, "playlist_refresh", min_hours=12) is True


def test_should_run_interval_recent_success_skips():
    recent = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    client = _Client([{"created_at": recent}])
    assert should_run_interval(client, "playlist_refresh", min_hours=12) is False


def test_hours_since_last_success_none_when_empty():
    client = _Client([])
    assert hours_since_last_success(client, "export") is None

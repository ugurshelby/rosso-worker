"""api_cooldowns servisi testleri — cap mantığı + is_blocked."""
from datetime import datetime, timedelta, timezone

from app.services import cooldown


class _FakeRPC:
    def __init__(self, data):
        self._data = data
    def execute(self):
        class _R:
            pass
        r = _R()
        r.data = self._data
        return r


class _FakeClient:
    """cooldown_get/set RPC çağrılarını yakalayan sahte client."""
    def __init__(self, get_rows):
        self.get_rows = get_rows
        self.set_calls = []
    def rpc(self, name, params):
        if name == "cooldown_get":
            return _FakeRPC(self.get_rows)
        if name == "cooldown_set":
            self.set_calls.append(params)
            return _FakeRPC(None)
        raise AssertionError(f"beklenmeyen rpc: {name}")


def test_not_blocked_when_no_row():
    client = _FakeClient(get_rows=[])
    blocked, remaining = cooldown.is_blocked(client, "spotify")
    assert blocked is False
    assert remaining == 0


def test_blocked_when_future():
    future = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    client = _FakeClient(get_rows=[{"blocked_until": future, "reason": "429_quota", "hit_count": 1}])
    blocked, remaining = cooldown.is_blocked(client, "spotify")
    assert blocked is True
    assert 1700 < remaining <= 1800  # ~30 dk


def test_not_blocked_when_past():
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    client = _FakeClient(get_rows=[{"blocked_until": past, "reason": "429_quota", "hit_count": 1}])
    blocked, remaining = cooldown.is_blocked(client, "spotify")
    assert blocked is False


def test_set_cooldown_caps_retry_after():
    client = _FakeClient(get_rows=[])
    # 13 saat (46800s) talep edilse bile 6 saat (21600s) ile sınırlanır
    used = cooldown.set_cooldown(client, "spotify", retry_after_s=46800, reason="429_quota")
    assert used == cooldown.MAX_COOLDOWN_S
    assert len(client.set_calls) == 1


def test_set_cooldown_respects_small_retry_after():
    client = _FakeClient(get_rows=[])
    used = cooldown.set_cooldown(client, "spotify", retry_after_s=120, reason="429_quota")
    assert used == 120

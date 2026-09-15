"""spotify_token — worker-tarafı Spotify access token okuma + refresh testleri."""
from datetime import datetime, timedelta, timezone

from app.services.spotify_token import get_valid_spotify_token


class _Query:
    def __init__(self, rows):
        self._rows = rows
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def update(self, payload):
        self._payload = payload
        return self
    def execute(self):
        return type("R", (), {"data": self._rows})()


class _Client:
    def __init__(self, rows):
        self._rows = rows
        self.updated_payload = None
    def table(self, name):
        q = _Query(self._rows)
        orig_execute = q.execute
        def execute():
            if hasattr(q, "_payload"):
                self.updated_payload = q._payload
            return orig_execute()
        q.execute = execute
        return q


def test_token_not_expired_returns_decrypted_access_token(monkeypatch):
    """Token süresi dolmamışsa refresh YAPILMAZ, direkt decrypt edilip döner."""
    import app.services.spotify_token as mod
    monkeypatch.setattr(mod, "decrypt_token", lambda ct, key: f"plain-{ct}")

    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    client = _Client([{
        "access_token": "enc_access", "refresh_token": "enc_refresh",
        "token_expires": future, "is_active": True,
    }])

    token = get_valid_spotify_token(client, "user1", "key", http=None)
    assert token == "plain-enc_access"


def test_token_expired_refreshes_and_updates_db(monkeypatch):
    """Token süresi dolmuşsa refresh çağrılır, DB güncellenir, yeni token döner."""
    import app.services.spotify_token as mod
    monkeypatch.setattr(mod, "decrypt_token", lambda ct, key: f"plain-{ct}")
    monkeypatch.setattr(mod, "encrypt_token", lambda pt, key: f"enc-{pt}")

    class _Resp:
        status_code = 200
        def json(self):
            return {"access_token": "new_access", "expires_in": 3600}
        def raise_for_status(self): pass

    class _Http:
        def post(self, url, data=None, auth=None, timeout=10):
            return _Resp()

    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    client = _Client([{
        "access_token": "enc_old", "refresh_token": "enc_refresh",
        "token_expires": past, "is_active": True,
    }])

    token = get_valid_spotify_token(client, "user1", "key", http=_Http())
    assert token == "new_access"
    assert client.updated_payload["access_token"] == "enc-new_access"


def test_no_connection_returns_none():
    """Bağlantı yoksa None döner."""
    client = _Client([])
    token = get_valid_spotify_token(client, "user1", "key", http=None)
    assert token is None

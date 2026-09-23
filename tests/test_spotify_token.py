"""spotify_token — worker-tarafı Spotify access token okuma + refresh testleri."""
from datetime import datetime, timedelta, timezone

from app.services.spotify_token import get_valid_spotify_token, _resolve_client_credentials


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
    # BYOC (2026-09-23): bu kullanıcının BYOC satırı yok → paylaşılana düşer;
    # o yol env değişkenlerini okur, testte gerçek env'e bağımlı olmasın.
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "shared-id")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "shared-secret")

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


# ─── BYOC (2026-09-23) ──────────────────────────────────────────────────────
# `_Client` tüm tabloları AYNI satırlarla döndürüyor (yukarıdaki testler bunu
# varsayıyor); BYOC çözümlemesi ayrı bir tablo okuduğu için burada tabloya
# göre farklı veri döndüren küçük bir mock gerekiyor.

class _MultiTableClient:
    def __init__(self, tables: dict):
        self._tables = tables

    def table(self, name):
        return _Query(self._tables.get(name, []))


def test_byoc_verified_credentials_used_over_shared(monkeypatch):
    """Doğrulanmış BYOC satırı varsa paylaşılan env DEĞİL, onun kimliği kullanılır."""
    import app.services.spotify_token as mod
    monkeypatch.setattr(mod, "decrypt_token", lambda ct, key: f"plain-{ct}")
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "shared-id")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "shared-secret")

    client = _MultiTableClient({
        "spotify_byoc_credentials": [{
            "client_id": "user-own-id",
            "client_secret": "enc_user_secret",
            "verified_at": "2026-09-23T00:00:00+00:00",
        }],
    })

    creds = _resolve_client_credentials(client, "user1", "key")
    assert creds == ("user-own-id", "plain-enc_user_secret")


def test_byoc_unverified_row_falls_back_to_shared(monkeypatch):
    """`verified_at` boşsa BYOC satırı YOK sayılır — yanlış sırla kilitlenme yok."""
    import app.services.spotify_token as mod
    monkeypatch.setattr(mod, "decrypt_token", lambda ct, key: f"plain-{ct}")
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "shared-id")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "shared-secret")

    client = _MultiTableClient({
        "spotify_byoc_credentials": [{
            "client_id": "user-own-id",
            "client_secret": "enc_user_secret",
            "verified_at": None,
        }],
    })

    creds = _resolve_client_credentials(client, "user1", "key")
    assert creds == ("shared-id", "shared-secret")


def test_no_byoc_row_and_no_shared_env_returns_none(monkeypatch):
    """Ne BYOC ne paylaşılan env varsa None döner — çağıran taraf refresh'i durdurur."""
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    monkeypatch.delenv("SPOTIFY_CLIENT_SECRET", raising=False)

    client = _MultiTableClient({"spotify_byoc_credentials": []})
    creds = _resolve_client_credentials(client, "user1", "key")
    assert creds is None

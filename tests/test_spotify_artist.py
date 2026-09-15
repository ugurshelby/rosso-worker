"""Spotify /tracks ve /artists genre çekimi testleri (httpx mock'lu)."""
from app.services import spotify_artist


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.headers = {}
    def json(self):
        return self._payload
    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("err", request=None, response=self)


class _HTTP:
    """Sıralı yanıt döndüren sahte httpx client."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
    def get(self, url, **kwargs):
        self.calls.append(url)
        return self._responses.pop(0)
    def post(self, *a, **k):
        # token endpoint
        return _Resp({"access_token": "tok", "expires_in": 3600})


def test_get_track_artist_ids_extracts_ids(monkeypatch):
    monkeypatch.setattr(spotify_artist, "_get_access_token", lambda *a, **k: "tok")
    http = _HTTP([
        _Resp({"id": "t1", "artists": [{"id": "a1"}, {"id": "a2"}]}),
    ])
    result, quota = spotify_artist.get_track_artist_ids(["t1"], "cid", "sec", http)
    assert result == {"t1": ["a1", "a2"]}
    assert quota is False


def test_get_artist_genres_extracts_genres(monkeypatch):
    monkeypatch.setattr(spotify_artist, "_get_access_token", lambda *a, **k: "tok")
    http = _HTTP([
        _Resp({"id": "a1", "genres": ["turkish pop", "pop"]}),
    ])
    result, quota = spotify_artist.get_artist_genres(["a1"], "cid", "sec", http)
    assert result == {"a1": ["turkish pop", "pop"]}
    assert quota is False


def test_quota_hit_breaks_loop(monkeypatch):
    from app.services.spotify_lookup import SpotifyQuotaExhausted
    monkeypatch.setattr(spotify_artist, "_get_access_token", lambda *a, **k: "tok")

    def _boom(*a, **k):
        raise SpotifyQuotaExhausted("quota")
    monkeypatch.setattr(spotify_artist, "_with_retry", _boom)

    http = _HTTP([_Resp({"id": "a1", "genres": []})])
    result, quota = spotify_artist.get_artist_genres(["a1", "a2"], "cid", "sec", http)
    assert quota is True
    assert result == {}

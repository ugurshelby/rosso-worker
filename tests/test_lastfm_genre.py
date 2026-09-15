"""lastfm_genre — artist+title → top tags testleri (fake http)."""
from app.services.lastfm_genre import get_lastfm_tags


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
    def raise_for_status(self):
        pass
    def json(self):
        return self._payload


class _FakeHttp:
    """method param'a göre track/artist top tags döndürür."""
    def __init__(self, track_payload, artist_payload):
        self._track = track_payload
        self._artist = artist_payload
        self.calls = []
    def get(self, url, **kwargs):
        self.calls.append(url)
        if "track.gettoptags" in url.lower():
            return _FakeResp(self._track)
        if "artist.gettoptags" in url.lower():
            return _FakeResp(self._artist)
        return _FakeResp({})


def test_track_tags_doner():
    http = _FakeHttp(
        track_payload={"toptags": {"tag": [{"name": "pop"}, {"name": "dance"}]}},
        artist_payload={},
    )
    tags = get_lastfm_tags("Dua Lipa", "New Rules", "KEY", http)
    assert "pop" in tags


def test_track_bos_artist_fallback():
    # track tag boş → artist.getTopTags devreye girer
    http = _FakeHttp(
        track_payload={"toptags": {"tag": []}},
        artist_payload={"toptags": {"tag": [{"name": "arabesk"}, {"name": "turkish"}]}},
    )
    tags = get_lastfm_tags("Müslüm Gürses", "Affet", "KEY", http)
    assert "arabesk" in tags
    assert any("artist.gettoptags" in c.lower() for c in http.calls)


def test_hicbiri_bos():
    http = _FakeHttp(track_payload={"toptags": {"tag": []}}, artist_payload={"toptags": {"tag": []}})
    assert get_lastfm_tags("Yok", "Yok", "KEY", http) == []


def test_lastfm_track_429_raises_ratelimit():
    """Last.fm track.getTopTags 429 → RateLimitError(provider='lastfm')."""
    import httpx
    import pytest
    from app.services.lastfm_genre import get_lastfm_track_tags_scored
    from app.services.genre_errors import RateLimitError

    class _Resp:
        status_code = 429
        headers = {}
        def raise_for_status(self):
            raise httpx.HTTPStatusError("429", request=None, response=self)
        def json(self): return {}

    class _Http:
        def get(self, url, timeout=10): return _Resp()

    with pytest.raises(RateLimitError) as exc:
        get_lastfm_track_tags_scored("A", "B", "key", _Http())
    assert exc.value.provider == "lastfm"
    assert exc.value.retry_after is None

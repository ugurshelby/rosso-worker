"""musicbrainz_genre — recording + artist tür çekimi testleri (mock http)."""
import pytest

import app.services.musicbrainz_genre as mb
from app.services.musicbrainz_genre import (
    get_musicbrainz_recording_genres,
    get_musicbrainz_artist_genres,
)


@pytest.fixture(autouse=True)
def _no_rate_limit_sleep(monkeypatch):
    """Testlerde MB rate-limit uykusunu devre dışı bırak (aksi halde her istek 1.1sn)."""
    monkeypatch.setattr(mb, "_RATE_LIMIT_SLEEP", 0)


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeHttp:
    """Sıralı yanıt kuyruğu; her get() bir sonraki yanıtı döner."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, timeout=None, headers=None):
        self.calls.append(url)
        return self._responses.pop(0)


# ─────────────────────────────────────────────────────────────────────
# Recording: search → MBID → lookup(inc=genres)
# ─────────────────────────────────────────────────────────────────────
def test_recording_search_sonra_lookup_genres():
    search = _FakeResp({"recordings": [{"id": "rec-mbid"}]})
    lookup = _FakeResp({"genres": [{"name": "hip hop", "count": 3},
                                   {"name": "pop rap", "count": 1}]})
    http = _FakeHttp([search, lookup])
    result = get_musicbrainz_recording_genres("Drake", "God's Plan", http)
    names = [name for name, _count in result]
    assert "hip hop" in names
    assert "pop rap" in names
    assert len(http.calls) == 2


def test_recording_genres_bos_ise_tags_fallback():
    search = _FakeResp({"recordings": [{"id": "rec-mbid"}]})
    lookup = _FakeResp({"genres": [], "tags": [{"name": "rap", "count": 2}]})
    http = _FakeHttp([search, lookup])
    result = get_musicbrainz_recording_genres("X", "Y", http)
    names = [name for name, _ in result]
    assert "rap" in names


def test_recording_bulunamazsa_bos():
    search = _FakeResp({"recordings": []})
    http = _FakeHttp([search])
    assert get_musicbrainz_recording_genres("X", "Y", http) == []
    # tek istek yapıldı (lookup'a gerek yok)
    assert len(http.calls) == 1


def test_recording_bos_giris():
    http = _FakeHttp([])
    assert get_musicbrainz_recording_genres("", "Y", http) == []
    assert get_musicbrainz_recording_genres("X", "", http) == []
    assert len(http.calls) == 0


def test_recording_http_hatasi_bos_doner():
    search = _FakeResp({}, status=503)
    http = _FakeHttp([search])
    assert get_musicbrainz_recording_genres("X", "Y", http) == []


# ─────────────────────────────────────────────────────────────────────
# Artist: search → MBID → lookup(inc=genres)
# ─────────────────────────────────────────────────────────────────────
def test_artist_search_sonra_lookup_genres():
    search = _FakeResp({"artists": [{"id": "art-mbid"}]})
    lookup = _FakeResp({"genres": [{"name": "hip hop", "count": 17},
                                   {"name": "r&b", "count": 7}]})
    http = _FakeHttp([search, lookup])
    result = get_musicbrainz_artist_genres("Drake", http)
    names = [name for name, _ in result]
    assert "hip hop" in names
    assert "r&b" in names
    assert len(http.calls) == 2


def test_artist_bulunamazsa_bos():
    search = _FakeResp({"artists": []})
    http = _FakeHttp([search])
    assert get_musicbrainz_artist_genres("Nonexistent", http) == []


def test_artist_bos_giris():
    http = _FakeHttp([])
    assert get_musicbrainz_artist_genres("", http) == []
    assert len(http.calls) == 0


def test_user_agent_header_gonderilir():
    search = _FakeResp({"artists": [{"id": "m"}]})
    lookup = _FakeResp({"genres": [{"name": "pop", "count": 1}]})

    captured = {}

    class _HeaderHttp:
        def get(self, url, timeout=None, headers=None):
            captured["headers"] = headers
            return search if "query" in url else lookup

    get_musicbrainz_artist_genres("X", _HeaderHttp())
    assert captured["headers"] is not None
    assert "User-Agent" in captured["headers"]


def test_get_serializes_with_min_interval(monkeypatch):
    """İki ardışık _get çağrısı arasında en az _RATE_LIMIT_SLEEP kadar beklenir (kilit)."""
    import app.services.musicbrainz_genre as mb

    monkeypatch.setattr(mb, "_RATE_LIMIT_SLEEP", 0.05)
    mb._reset_rate_limit_state()  # temiz başla

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {}

    class _Http:
        def get(self, url, timeout=10, headers=None): return _Resp()

    http = _Http()
    import time as _t
    t0 = _t.monotonic()
    mb._get(http, "https://musicbrainz.org/ws/2/artist?query=x&fmt=json")
    mb._get(http, "https://musicbrainz.org/ws/2/artist?query=y&fmt=json")
    elapsed = _t.monotonic() - t0
    assert elapsed >= 0.05


def test_rate_limit_state_reset_helper():
    """_reset_rate_limit_state test yardımcısı mevcut."""
    import app.services.musicbrainz_genre as mb
    mb._reset_rate_limit_state()  # hata vermemeli

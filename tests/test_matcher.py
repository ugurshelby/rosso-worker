"""Track matcher testleri — track-matching-sync-research.md §1, §2.

Matcher DB'den aday çeker; testte sahte bir TrackRepo enjekte ederiz
(saf eşleştirme mantığını izole test etmek için).
"""
from app.matching.matcher import match_track, MatchResult


class FakeRepo:
    """Sahte tracks deposu — matcher'a enjekte edilir."""

    def __init__(self, by_spotify=None, by_isrc=None, fuzzy=None):
        self._by_spotify = by_spotify or {}
        self._by_isrc = by_isrc or {}
        self._fuzzy = fuzzy or []

    def find_by_spotify_id(self, spotify_id):
        return self._by_spotify.get(spotify_id)

    def find_by_isrc(self, isrc):
        return self._by_isrc.get(isrc)

    def find_fuzzy_candidates(self, title, artists):
        return self._fuzzy


def test_match_by_spotify_uri_first():
    repo = FakeRepo(by_spotify={"abc": {"id": "t1", "title": "Song"}})
    res = match_track(
        {"spotify_track_uri": "spotify:track:abc"}, repo
    )
    assert res.track_id == "t1"
    assert res.method == "uri"
    assert res.confidence == "high"


def test_match_by_isrc_when_no_uri_match():
    repo = FakeRepo(by_isrc={"USRC12345": {"id": "t2"}})
    res = match_track({"isrc": "USRC12345"}, repo)
    assert res.track_id == "t2"
    assert res.method == "isrc"
    assert res.confidence == "high"


def test_fuzzy_match_above_threshold():
    repo = FakeRepo(
        fuzzy=[{"id": "t3", "title": "Bohemian Rhapsody", "artists": ["Queen"], "duration_ms": 354000}]
    )
    res = match_track(
        {"title": "Bohemian Rhapsody", "artists": ["Queen"], "duration_ms": 354000},
        repo,
    )
    assert res.track_id == "t3"
    assert res.method == "fuzzy"
    assert res.confidence == "medium"


def test_no_match_returns_unmatched():
    repo = FakeRepo(fuzzy=[{"id": "x", "title": "Totally Different", "artists": ["Z"], "duration_ms": 100000}])
    res = match_track(
        {"title": "No Such Song", "artists": ["Nobody"], "duration_ms": 999000},
        repo,
    )
    assert res.track_id is None
    assert res.method == "unmatched"
    assert res.confidence == "none"


def test_podcast_without_isrc_falls_to_fuzzy_then_unmatched():
    # research §1: podcast'te ISRC yok → fuzzy → eşleşmezse unmatched (silinmez)
    repo = FakeRepo()
    res = match_track({"title": "Episode 5", "artists": []}, repo)
    assert res.track_id is None
    assert res.method == "unmatched"


def test_result_is_dataclass_like():
    repo = FakeRepo(by_isrc={"I": {"id": "t"}})
    res = match_track({"isrc": "I"}, repo)
    assert isinstance(res, MatchResult)

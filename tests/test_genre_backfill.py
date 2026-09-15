"""genre_backfill — artist profilinden güvenli tür doldurma testleri."""
from app.pipeline.genre_backfill import resolve_backfill_genre


def test_single_genre_with_db_tracks_source_is_used():
    """Tek baskın tür + db_tracks kaynağı varsa o tür döner."""
    profile = {
        "slots": ["trap"],
        "sources": {"trap": ["lastfm_artist", "db_tracks"]},
    }
    assert resolve_backfill_genre(profile) == "trap"


def test_single_genre_without_db_tracks_source_returns_none():
    """Tek tür var ama db_tracks kaynağı yoksa (başka track'te doğrulanmamış) → None."""
    profile = {
        "slots": ["hip-hop"],
        "sources": {"hip-hop": ["lastfm_artist"]},
    }
    assert resolve_backfill_genre(profile) is None


def test_multiple_genres_returns_none_even_with_db_tracks():
    """Birden fazla tür varsa (çakışma riski, örn. Azer Bülbül) → None, dokunma."""
    profile = {
        "slots": ["arabesk", "black metal"],
        "sources": {
            "arabesk": ["lastfm_artist", "db_tracks"],
            "black metal": ["lastfm_artist"],
        },
    }
    assert resolve_backfill_genre(profile) is None


def test_empty_profile_returns_none():
    """Boş profil (slots yok) → None."""
    assert resolve_backfill_genre({"slots": [], "sources": {}}) is None
    assert resolve_backfill_genre({}) is None


class _BackfillClient:
    """Fake Supabase client — tracks (failed) + artists (profil cache) tabloları."""
    def __init__(self, failed_tracks, artist_profiles):
        self._failed_tracks = failed_tracks
        self._artist_profiles = artist_profiles  # {name_normalized: genre_data dict}
        self.updated_tracks: list[dict] = []

    def table(self, name):
        client = self
        class _Q:
            def __init__(self):
                self._filters = {}
                self._payload = None
            def select(self, *a, **k): return self
            def is_(self, col, val): return self
            @property
            def not_(self):
                return self
            def eq(self, col, val):
                self._filters[col] = val
                return self
            def limit(self, n): return self
            def order(self, *a, **k): return self
            def update(self, payload):
                self._payload = payload
                return self
            def execute(self):
                if name == "tracks" and self._payload is not None:
                    client.updated_tracks.append({
                        "id": self._filters.get("id"), **self._payload,
                    })
                    return type("R", (), {"data": []})()
                if name == "tracks":
                    return type("R", (), {"data": client._failed_tracks})()
                if name == "artists":
                    key = self._filters.get("name_normalized")
                    profile = client._artist_profiles.get(key)
                    data = [{"genre_data": profile}] if profile else []
                    return type("R", (), {"data": data})()
                return type("R", (), {"data": []})()
        return _Q()


def test_backfill_fills_track_when_profile_qualifies():
    """Artist profili şartları sağlıyorsa track güncellenir."""
    from app.pipeline.genre_backfill import run_one_genre_backfill_batch

    client = _BackfillClient(
        failed_tracks=[{"id": "t1", "title": "129", "artists": ["Şehinşah"]}],
        artist_profiles={
            "şehinşah": {"slots": ["trap"], "sources": {"trap": ["lastfm_artist", "db_tracks"]}},
        },
    )
    result = run_one_genre_backfill_batch(client, batch_limit=50)

    assert result["outcome"] == "success"
    assert result["filled"] == 1
    assert len(client.updated_tracks) == 1
    assert client.updated_tracks[0]["genres"] == ["trap"]
    assert client.updated_tracks[0]["genre_source"] == "artist_profile_backfill"
    # genre_lookup_failed_at temizlenmeli (artık başarılı)
    assert client.updated_tracks[0]["genre_lookup_failed_at"] is None


def test_backfill_skips_track_when_profile_does_not_qualify():
    """Artist profili şartları sağlamıyorsa (çakışan türler) track dokunulmaz."""
    from app.pipeline.genre_backfill import run_one_genre_backfill_batch

    client = _BackfillClient(
        failed_tracks=[{"id": "t2", "title": "Duygularım", "artists": ["Azer Bülbül"]}],
        artist_profiles={
            "azer bülbül": {
                "slots": ["arabesk", "black metal"],
                "sources": {"arabesk": ["lastfm_artist"], "black metal": ["lastfm_artist"]},
            },
        },
    )
    result = run_one_genre_backfill_batch(client, batch_limit=50)

    assert result["filled"] == 0
    assert client.updated_tracks == []


def test_backfill_skips_track_when_no_artist_profile_cached():
    """Sanatçının hiç profili yoksa (henüz işlenmemiş) track dokunulmaz."""
    from app.pipeline.genre_backfill import run_one_genre_backfill_batch

    client = _BackfillClient(
        failed_tracks=[{"id": "t3", "title": "Random", "artists": ["Bilinmeyen Sanatçı"]}],
        artist_profiles={},
    )
    result = run_one_genre_backfill_batch(client, batch_limit=50)

    assert result["filled"] == 0
    assert client.updated_tracks == []


def test_backfill_empty_when_no_failed_tracks():
    from app.pipeline.genre_backfill import run_one_genre_backfill_batch

    client = _BackfillClient(failed_tracks=[], artist_profiles={})
    result = run_one_genre_backfill_batch(client, batch_limit=50)
    assert result["outcome"] == "empty"

"""Playlist & Library parser testleri."""
from app.parser.spotify_playlists import (
    parse_playlists,
    parse_library,
)


# ─── parse_playlists: Playlist*.json → playlist + track listesi ───

def test_parse_playlists_basic():
    data = {
        "playlists": [
            {
                "name": "Summer 2023",
                "lastModifiedDate": "2023-09-01",
                "items": [
                    {"track": {"trackName": "Song A", "artistName": "Artist A",
                               "trackUri": "spotify:track:A"}},
                    {"track": {"trackName": "Song B", "artistName": "Artist B",
                               "trackUri": "spotify:track:B"}},
                ],
            }
        ]
    }
    out = parse_playlists(data)
    assert len(out) == 1
    assert out[0]["name"] == "Summer 2023"
    assert len(out[0]["tracks"]) == 2
    assert out[0]["tracks"][0]["uri"] == "spotify:track:A"


def test_parse_playlists_skips_null_tracks():
    # local file / kaldırılmış track → track None olabilir
    data = {
        "playlists": [
            {"name": "Mix", "items": [
                {"track": None},
                {"track": {"trackName": "Real", "artistName": "X", "trackUri": "spotify:track:R"}},
            ]}
        ]
    }
    out = parse_playlists(data)
    assert len(out[0]["tracks"]) == 1


def test_parse_playlists_empty():
    assert parse_playlists({"playlists": []}) == []
    assert parse_playlists({}) == []


# ─── parse_library: YourLibrary.json → beğenilen track'ler ───

def test_parse_library_tracks():
    data = {
        "tracks": [
            {"artist": "Queen", "album": "A Night at the Opera",
             "track": "Bohemian Rhapsody", "uri": "spotify:track:BR"},
        ]
    }
    out = parse_library(data)
    assert len(out) == 1
    assert out[0]["uri"] == "spotify:track:BR"
    assert out[0]["track_name"] == "Bohemian Rhapsody"


def test_parse_library_empty():
    assert parse_library({}) == []

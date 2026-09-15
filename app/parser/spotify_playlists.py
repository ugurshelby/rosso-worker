"""Spotify export Playlist*.json & YourLibrary.json parser — saf fonksiyonlar."""
from __future__ import annotations

from typing import Any


def parse_playlists(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Playlist*.json → [{name, lastModified, tracks:[{name,artist,uri}]}].

    Null track'ler (kaldırılmış/local file) atlanır.
    """
    out: list[dict[str, Any]] = []
    for pl in data.get("playlists", []) or []:
        tracks = []
        for item in pl.get("items", []) or []:
            tr = item.get("track")
            if not tr:
                continue
            tracks.append(
                {
                    "name": tr.get("trackName"),
                    "artist": tr.get("artistName"),
                    "album": tr.get("albumName"),
                    "uri": tr.get("trackUri"),
                }
            )
        out.append(
            {
                "name": pl.get("name"),
                "last_modified": pl.get("lastModifiedDate"),
                "tracks": tracks,
            }
        )
    return out


def parse_library(data: dict[str, Any]) -> list[dict[str, Any]]:
    """YourLibrary.json → beğenilen şarkılar [{track_name, artist, album, uri}]."""
    out: list[dict[str, Any]] = []
    for tr in data.get("tracks", []) or []:
        out.append(
            {
                "track_name": tr.get("track"),
                "artist": tr.get("artist"),
                "album": tr.get("album"),
                "uri": tr.get("uri"),
            }
        )
    return out

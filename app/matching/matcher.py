"""Track matcher — ISRC öncelikli, fuzzy fallback.

Kaynak: track-matching-sync-research.md §1, §2 + platform-limits-research.md §11.
Eşleştirme önceliği:
  1. Spotify URI → spotify_id
  2. ISRC → isrc (local/podcast'te yok → fuzzy)
  3. title+artist fuzzy (skor ≥ 0.85)
  4. eşleşmezse track_id=None (silinmez, raw saklanır)

Repo (tracks deposu) dışarıdan enjekte edilir → saf mantık test-edilebilir.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.matching.normalize import MATCH_THRESHOLD, calculate_match_score


class TrackRepo(Protocol):
    """Matcher'ın ihtiyaç duyduğu tracks erişim arayüzü."""

    def find_by_spotify_id(self, spotify_id: str) -> dict[str, Any] | None: ...
    def find_by_isrc(self, isrc: str) -> dict[str, Any] | None: ...
    def find_fuzzy_candidates(
        self, title: str, artists: list[str]
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class MatchResult:
    track_id: str | None
    confidence: str  # "high" | "medium" | "none"
    method: str      # "uri" | "isrc" | "fuzzy" | "unmatched"


_UNMATCHED = MatchResult(track_id=None, confidence="none", method="unmatched")


def _spotify_id_from_uri(uri: str | None) -> str | None:
    if not uri:
        return None
    # "spotify:track:ID" → "ID"
    parts = uri.split(":")
    return parts[-1] if len(parts) == 3 and parts[1] == "track" else None


def match_track(source: dict[str, Any], repo: TrackRepo) -> MatchResult:
    """Tek bir kaynak track'i tracks kataloğuyla eşleştir."""
    # 1. Spotify URI → spotify_id
    spotify_id = source.get("spotify_id") or _spotify_id_from_uri(
        source.get("spotify_track_uri")
    )
    if spotify_id:
        hit = repo.find_by_spotify_id(spotify_id)
        if hit:
            return MatchResult(hit["id"], "high", "uri")

    # 2. ISRC (local files / podcast'te yok — research §1)
    isrc = source.get("isrc")
    if isrc:
        hit = repo.find_by_isrc(isrc)
        if hit:
            return MatchResult(hit["id"], "high", "isrc")

    # 3. title + artist fuzzy (skor ≥ 0.85)
    title = source.get("title") or source.get("raw_track_name")
    if not title:
        return _UNMATCHED
    artists = source.get("artists") or (
        [source["raw_artist_name"]] if source.get("raw_artist_name") else []
    )
    candidates = repo.find_fuzzy_candidates(title, artists)
    best: tuple[float, dict[str, Any]] | None = None
    src = {"title": title, "artists": artists, "duration_ms": source.get("duration_ms")}
    for cand in candidates:
        score = calculate_match_score(src, cand)
        if score >= MATCH_THRESHOLD and (best is None or score > best[0]):
            best = (score, cand)
    if best:
        return MatchResult(best[1]["id"], "medium", "fuzzy")

    # 4. eşleşme yok — silinmez, raw saklanır
    return _UNMATCHED

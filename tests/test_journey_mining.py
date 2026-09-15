"""Unit tests for Journey Latent Data Mining Engine (Stage 1).

Tests all 5 latent dimensions:
  1. Intentionality Quotient (Agency Score) with 30-min passive cluster decay
  2. Temporal Displacement Index (Delta t) with chronological focus tags
  3. Obsession Topology (Comets vs. Pillars) with kurtosis & active lifespan
  4. Auditory Rejection Signature (Skip Velocity, Dopamine Restlessness, Talisman Tracks)
  5. Circadian Drift (Daylight Arc centroid hour in Europe/Istanbul)
  6. Chromatic Palette Extraction (4-color: primary, glow, accent, deep)
"""
from datetime import datetime, timedelta, timezone
import re

from app.pipeline.journey_mining import (
    calculate_agency_score,
    calculate_temporal_displacement,
    calculate_obsession_topology,
    calculate_rejection_signature,
    calculate_circadian_drift,
    extract_chromatic_palette,
    mine_year_latent_dimensions,
)


def _dt_iso(year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> str:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc).isoformat()


# ─── 1. Agency Score Tests ──────────────────────────────────────────────────

def test_agency_score_empty():
    res = calculate_agency_score([])
    assert res["agency_score"] == 0.0
    assert res["total_plays"] == 0


def test_agency_score_active_interactions():
    # 10 plays, 4 clickrows, 2 backbtn, shuffle_rate = 0.0
    # Expected numerator = 4 + (2 * 1.5) = 7.0
    # ratio = 7.0 / 10 = 0.70
    # Agency Score = 0.70 * (1 - 0) = 0.70
    events = [
        {"played_at": _dt_iso(2025, 3, 1, 10, i * 2), "reason_start": "clickrow" if i < 4 else "trackdone",
         "reason_end": "backbtn" if i in (4, 5) else "trackdone", "shuffle": False}
        for i in range(10)
    ]
    res = calculate_agency_score(events)
    assert res["agency_score"] == 0.70
    assert res["clickrow_count"] == 4
    assert res["backbtn_count"] == 2
    assert res["shuffle_rate"] == 0.0


def test_agency_score_shuffle_penalty():
    # 10 plays, all clickrow (10.0), shuffle = True (shuffle_rate = 1.0)
    # Agency Score = 1.0 * (1 - 0.4 * 1.0) = 0.60
    events = [
        {"played_at": _dt_iso(2025, 3, 1, 10, i * 2), "reason_start": "clickrow", "shuffle": True}
        for i in range(10)
    ]
    res = calculate_agency_score(events)
    assert res["agency_score"] == 0.60
    assert res["shuffle_rate"] == 1.0


def test_agency_score_passive_cluster_decay():
    # First track: clickrow at 10:00
    # Tracks 2..5: trackdone with no manual actions, within 30 min -> decays into passive cluster
    # Track 6: after 45 min (> 30 min gap) -> new session
    events = [
        {"played_at": _dt_iso(2025, 3, 1, 10, 0), "reason_start": "clickrow", "reason_end": "trackdone"},
        {"played_at": _dt_iso(2025, 3, 1, 10, 5), "reason_start": "trackdone", "reason_end": "trackdone"},
        {"played_at": _dt_iso(2025, 3, 1, 10, 10), "reason_start": "trackdone", "reason_end": "trackdone"},
        {"played_at": _dt_iso(2025, 3, 1, 10, 15), "reason_start": "trackdone", "reason_end": "trackdone"},
        # New session starts at 11:15 (> 30 min gap)
        {"played_at": _dt_iso(2025, 3, 1, 11, 15), "reason_start": "clickrow", "reason_end": "trackdone"},
    ]
    res = calculate_agency_score(events)
    assert res["session_count"] == 2
    assert res["passive_play_count"] == 3
    assert res["active_play_count"] == 2
    assert res["clickrow_count"] == 2


# ─── 2. Temporal Displacement Tests ─────────────────────────────────────────

def test_temporal_displacement_contemporary():
    # Year 2025 plays of 2025 tracks -> Delta t = 0
    events = [
        {"played_at": _dt_iso(2025, 4, 1), "release_year": 2025}
        for _ in range(10)
    ]
    res = calculate_temporal_displacement(events, 2025)
    assert res["tag"] == "contemporary"
    assert res["mean_displacement"] == 0.0
    assert res["contemporary_share"] == 1.0


def test_temporal_displacement_nostalgia():
    # Year 2025 plays of 2005 tracks -> Delta t = 20 (Nostalgia [15, 25])
    events = [
        {"played_at": _dt_iso(2025, 4, 1), "release_year": 2005}
        for _ in range(10)
    ]
    res = calculate_temporal_displacement(events, 2025)
    assert res["tag"] == "nostalgia"
    assert res["mean_displacement"] == 20.0
    assert res["nostalgia_share"] == 1.0


def test_temporal_displacement_archival():
    # Year 2025 plays of 1970 tracks -> Delta t = 55 (Archival >= 40)
    events = [
        {"played_at": _dt_iso(2025, 4, 1), "release_year": 1970}
        for _ in range(10)
    ]
    res = calculate_temporal_displacement(events, 2025)
    assert res["tag"] == "archival"
    assert res["mean_displacement"] == 55.0
    assert res["archival_share"] == 1.0


# ─── 3. Obsession Topology Tests ────────────────────────────────────────────

def test_obsession_topology_comet():
    # Comet: 45 plays within 10 days in June 2025 (span < 30d, concentration > 40)
    events = [
        {
            "track_id": "comet-1",
            "title": "Flash Star",
            "artist": "Nova",
            "played_at": _dt_iso(2025, 6, 1 + (i % 10), hour=14),
        }
        for i in range(45)
    ]
    res = calculate_obsession_topology(events, min_comet_plays=40)
    assert res["comet_count"] >= 1
    top_comet = res["comets"][0]
    assert top_comet["track_id"] == "comet-1"
    assert top_comet["span_days"] < 30
    assert top_comet["plays"] == 45


def test_obsession_topology_pillar():
    pillar_candidates = [
        {
            "track_id": "pillar-1",
            "title": "Enduring Anchor",
            "artist": "Legend",
            "active_months": 28,
            "skip_rate": 0.05,
            "total_plays": 240,
            "first_played_at": "2023-01-01T00:00:00Z",
            "last_played_at": "2025-05-01T00:00:00Z",
        }
    ]
    res = calculate_obsession_topology([], pillar_candidates=pillar_candidates)
    assert res["pillar_count"] == 1
    assert res["pillars"][0]["track_id"] == "pillar-1"
    assert res["pillars"][0]["active_months"] == 28


# ─── 4. Auditory Rejection Signature Tests ──────────────────────────────────

def test_rejection_signature_immediate_and_restlessness():
    # 5 consecutive skips in 30 seconds (< 45s) -> Dopamine Restlessness
    events = [
        {
            "track_id": f"t-{i}",
            "played_at": (datetime(2025, 5, 1, 12, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=i * 6)).isoformat(),
            "ms_played": 3000,  # < 5s -> immediate rejection
            "skipped": True,
            "reason_end": "fwdbtn",
        }
        for i in range(5)
    ]
    res = calculate_rejection_signature(events)
    assert res["immediate_rejections"] == 5
    assert res["dopamine_restlessness_events"] == 1


def test_rejection_signature_talisman_tracks():
    # Track with 30 plays and 0 skips
    events = [
        {
            "track_id": "talisman-gold",
            "title": "Sacred Track",
            "artist": "Immortal",
            "played_at": _dt_iso(2025, 7, 1, hour=i % 24),
            "ms_played": 180000,
            "skipped": False,
            "reason_end": "trackdone",
        }
        for i in range(30)
    ]
    res = calculate_rejection_signature(events)
    assert len(res["talisman_tracks"]) == 1
    assert res["talisman_tracks"][0]["track_id"] == "talisman-gold"
    assert res["talisman_tracks"][0]["skip_rate"] == 0.0


# ─── 5. Circadian Drift Tests ───────────────────────────────────────────────

def test_circadian_drift_centroid_istanbul():
    # Istanbul is UTC+3.
    # A play at UTC 12:00 corresponds to Istanbul local 15:00.
    events = [
        {"played_at": _dt_iso(2025, 8, 1, hour=12, minute=0)}
        for _ in range(10)
    ]
    res = calculate_circadian_drift(events)
    assert res["centroid_hour"] == 15.0
    assert res["peak_hour"] == 15
    assert res["hourly_distribution"][15] == 10


# ─── 6. Chromatic Palette Extraction Tests ──────────────────────────────────

def test_extract_chromatic_palette():
    covers = [
        {"track_id": "c1", "title": "Album 1", "image_url": "https://example.com/1.jpg"},
        {"track_id": "c2", "title": "Album 2", "image_url": "https://example.com/2.jpg"},
        {"track_id": "c3", "title": "Album 3", "image_url": "https://example.com/3.jpg"},
    ]
    palette = extract_chromatic_palette(covers, year=2025, dominant_genre="Rock")
    assert "primary" in palette
    assert "glow" in palette
    assert "accent" in palette
    assert "deep" in palette

    hex_re = re.compile(r"^#[0-9a-f]{6}$", re.IGNORECASE)
    assert hex_re.match(palette["primary"])
    assert hex_re.match(palette["glow"])
    assert hex_re.match(palette["accent"])
    assert hex_re.match(palette["deep"])


def test_extract_chromatic_palette_missing_artwork_fallback():
    # When covers are empty or have missing artwork, must fall back to #08040C
    empty_palette = extract_chromatic_palette([], year=2025)
    assert empty_palette["deep"] == "#08040C"

    missing_art_palette = extract_chromatic_palette([{"title": "No Art", "image_url": None}], year=2025)
    assert missing_art_palette["deep"] == "#08040C"


# ─── 7. Full Mining Orchestration ───────────────────────────────────────────

def test_mine_year_latent_dimensions():
    events = [
        {
            "track_id": "t-1",
            "played_at": _dt_iso(2025, 5, 1, 10, 0),
            "ms_played": 120000,
            "reason_start": "clickrow",
            "reason_end": "trackdone",
            "shuffle": False,
            "skipped": False,
            "release_year": 2025,
            "duration_ms": 120000,
            "title": "Track 1",
            "artist": "Artist 1",
        }
    ]
    pkg = mine_year_latent_dimensions(2025, events, covers=[])
    assert "latent" in pkg
    assert "palette" in pkg
    assert "intentionality" in pkg["latent"]
    assert "temporal_displacement" in pkg["latent"]
    assert "obsession_topology" in pkg["latent"]
    assert "rejection_signature" in pkg["latent"]
    assert "circadian_drift" in pkg["latent"]

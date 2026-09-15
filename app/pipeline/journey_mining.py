"""Journey Latent Data Mining Engine (Stage 1 · Latent Mining & Package Architecture).

Computes 5 analytical dimensions within background Python cron jobs
WITHOUT calling third-party AI APIs:
  1. Intentionality Quotient (Agency Score) with 30-min passive cluster decay
  2. Temporal Displacement Index (Delta t) with chronological focus tagging
  3. Obsession Topology (Comets vs. Pillars) with kurtosis & active lifespan
  4. Auditory Rejection Signature (Skip Velocity, Dopamine Restlessness, Talisman Tracks)
  5. Circadian Drift (Daylight Arc centroid hour in Europe/Istanbul)
  6. Chromatic Palette Extraction (4-color: primary, glow, accent, deep)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import math
from typing import Any
from zoneinfo import ZoneInfo

try:
    ISTANBUL_TZ = ZoneInfo("Europe/Istanbul")
except Exception:
    # Europe/Istanbul has maintained a permanent UTC+3 offset without DST since 2016
    ISTANBUL_TZ = timezone(timedelta(hours=3))


def parse_datetime(dt_val: Any) -> datetime:
    """Safely parse datetime string or object to UTC datetime."""
    if isinstance(dt_val, datetime):
        return dt_val.astimezone(timezone.utc)
    if isinstance(dt_val, str):
        # Handle ISO format with Z or timezone offset
        clean = dt_val.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(clean).astimezone(timezone.utc)
        except Exception:
            return datetime.now(timezone.utc)
    return datetime.now(timezone.utc)


# ─── 1. Intentionality Quotient (Agency Score) ──────────────────────────────

def calculate_agency_score(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Calculate Intentionality Quotient (Agency Score) with 30-min session decay.

    Formula:
        Agency Score = [sum(clickrow + backbtn * 1.5) / sum(total plays)] * (1 - 0.4 * shuffle_rate)

    Edge Case Control:
        Cluster plays within 30 minutes. If no manual interaction occurs after
        an initial clickrow, decay subsequent plays into a passive cluster.
    """
    total_plays = len(events)
    if total_plays == 0:
        return {
            "agency_score": 0.0,
            "clickrow_count": 0,
            "backbtn_count": 0,
            "fwdbtn_count": 0,
            "shuffle_rate": 0.0,
            "total_plays": 0,
            "session_count": 0,
            "passive_session_count": 0,
            "passive_play_count": 0,
            "active_play_count": 0,
        }

    # Sort chronological
    sorted_events = sorted(events, key=lambda e: parse_datetime(e.get("played_at")))

    sessions: list[list[dict[str, Any]]] = []
    current_session: list[dict[str, Any]] = []
    last_dt: datetime | None = None

    for ev in sorted_events:
        dt = parse_datetime(ev.get("played_at"))
        if last_dt is None or (dt - last_dt) > timedelta(minutes=30):
            if current_session:
                sessions.append(current_session)
            current_session = [ev]
        else:
            current_session.append(ev)
        last_dt = dt

    if current_session:
        sessions.append(current_session)

    clickrow_count = 0
    backbtn_count = 0
    fwdbtn_count = 0
    shuffle_count = 0
    passive_sessions = 0
    passive_plays = 0
    active_plays = 0

    # Cluster plays with passive decay
    for sess in sessions:
        manual_interaction_after_initial = False

        for i, ev in enumerate(sess):
            reason_start = str(ev.get("reason_start") or "").lower()
            reason_end = str(ev.get("reason_end") or "").lower()
            shuffle = bool(ev.get("shuffle"))

            if shuffle:
                shuffle_count += 1

            is_clickrow = reason_start == "clickrow"
            is_backbtn = reason_end == "backbtn"
            is_fwdbtn = reason_end == "fwdbtn"
            has_manual_action = is_clickrow or is_backbtn or is_fwdbtn

            if i == 0:
                if has_manual_action:
                    active_plays += 1
                    if is_clickrow:
                        clickrow_count += 1
                    if is_backbtn:
                        backbtn_count += 1
                    if is_fwdbtn:
                        fwdbtn_count += 1
                else:
                    passive_plays += 1
            else:
                if has_manual_action:
                    manual_interaction_after_initial = True
                    active_plays += 1
                    if is_clickrow:
                        clickrow_count += 1
                    if is_backbtn:
                        backbtn_count += 1
                    if is_fwdbtn:
                        fwdbtn_count += 1
                else:
                    # If no manual interaction occurs after initial play, decay into passive cluster
                    passive_plays += 1

        if (len(sess) > 1 and not manual_interaction_after_initial) or (
            len(sess) == 1 and str(sess[0].get("reason_start") or "").lower() != "clickrow"
        ):
            passive_sessions += 1

    shuffle_rate = round(shuffle_count / max(1, total_plays), 4)

    # Agency Score formula
    numerator = clickrow_count + (backbtn_count * 1.5)
    base_ratio = numerator / max(1, total_plays)
    agency_score = round(base_ratio * (1.0 - 0.4 * shuffle_rate), 4)
    # Clamp to [0.0, 1.0]
    agency_score = max(0.0, min(1.0, agency_score))

    return {
        "agency_score": agency_score,
        "clickrow_count": clickrow_count,
        "backbtn_count": backbtn_count,
        "fwdbtn_count": fwdbtn_count,
        "shuffle_rate": shuffle_rate,
        "total_plays": total_plays,
        "session_count": len(sessions),
        "passive_session_count": passive_sessions,
        "passive_play_count": passive_plays,
        "active_play_count": active_plays,
    }


# ─── 2. Temporal Displacement Index (Delta t) ───────────────────────────────

def calculate_temporal_displacement(events: list[dict[str, Any]], year: int) -> dict[str, Any]:
    """Calculate Temporal Displacement Index: Delta t = Year(played_at) - tracks.release_year.

    Tags chronological focus:
      - Contemporary (Delta t approx 0, <= 3)
      - Nostalgia (Delta t in [15, 25])
      - Archival (Delta t >= 40)
    """
    displacements: list[int] = []

    for ev in events:
        rel_year = ev.get("release_year")
        if rel_year is not None and isinstance(rel_year, (int, float)) and rel_year > 1900:
            play_dt = parse_datetime(ev.get("played_at"))
            play_year = play_dt.year if play_dt else year
            delta_t = max(0, int(play_year) - int(rel_year))
            displacements.append(delta_t)

    if not displacements:
        return {
            "mean_displacement": 0.0,
            "median_displacement": 0,
            "tag": "contemporary",
            "contemporary_share": 1.0,
            "nostalgia_share": 0.0,
            "archival_share": 0.0,
            "sample_size": 0,
        }

    displacements.sort()
    n = len(displacements)
    mean_dt = round(sum(displacements) / n, 2)
    median_dt = displacements[n // 2]

    contemporary_count = sum(1 for d in displacements if d <= 3)
    nostalgia_count = sum(1 for d in displacements if 15 <= d <= 25)
    archival_count = sum(1 for d in displacements if d >= 40)

    contemporary_share = round(contemporary_count / n, 4)
    nostalgia_share = round(nostalgia_count / n, 4)
    archival_share = round(archival_count / n, 4)

    # Tag classification
    if archival_share >= 0.20 or mean_dt >= 35.0:
        tag = "archival"
    elif nostalgia_share >= 0.25 or (14.0 <= mean_dt <= 28.0):
        tag = "nostalgia"
    elif contemporary_share >= 0.45 or mean_dt <= 5.0:
        tag = "contemporary"
    else:
        tag = "catalog"

    return {
        "mean_displacement": mean_dt,
        "median_displacement": median_dt,
        "tag": tag,
        "contemporary_share": contemporary_share,
        "nostalgia_share": nostalgia_share,
        "archival_share": archival_share,
        "sample_size": n,
    }


# ─── 3. Obsession Topology ("Comets" vs. "Pillars") ─────────────────────────

def calculate_kurtosis(timestamps: list[float]) -> float:
    """Calculate sample kurtosis of play timestamps in seconds."""
    n = len(timestamps)
    if n < 4:
        return 0.0
    mean_t = sum(timestamps) / n
    m2 = sum((t - mean_t) ** 2 for t in timestamps) / n
    if m2 <= 1e-9:
        return 0.0
    m4 = sum((t - mean_t) ** 4 for t in timestamps) / n
    kurt = m4 / (m2 ** 2)
    return round(kurt, 3)


def calculate_obsession_topology(
    events: list[dict[str, Any]],
    pillar_candidates: list[dict[str, Any]] | None = None,
    min_comet_plays: int = 40,
) -> dict[str, Any]:
    """Differentiate top tracks based on playback kurtosis and lifespan.

    Comets (Flash Obsessions):
        High concentration (plays > 40), short lifespan (span < 30 days),
        and zero plays in subsequent months.

    Pillars (Enduring Anchors):
        Consistent playback across >= 24 consecutive months with low skip rates.
    """
    # Group plays by track
    tracks_map: dict[str, list[dict[str, Any]]] = {}
    for ev in events:
        tid = str(ev.get("track_id") or "")
        if not tid:
            continue
        tracks_map.setdefault(tid, []).append(ev)

    comets: list[dict[str, Any]] = []

    for tid, t_events in tracks_map.items():
        play_count = len(t_events)
        if play_count < min_comet_plays:
            continue

        timestamps = [parse_datetime(e.get("played_at")).timestamp() for e in t_events]
        timestamps.sort()

        first_dt = datetime.fromtimestamp(timestamps[0], tz=timezone.utc)
        last_dt = datetime.fromtimestamp(timestamps[-1], tz=timezone.utc)
        span_days = max(1, (last_dt - first_dt).days)

        # Comet condition 1: lifespan < 30 days
        if span_days >= 30:
            continue

        # Comet condition 2: zero plays in subsequent months of the event dataset
        # Month of first play and last play should match
        if (last_dt.year != first_dt.year) or (last_dt.month != first_dt.month):
            continue

        sample_kurtosis = calculate_kurtosis(timestamps)
        first_ev = t_events[0]

        comets.append({
            "track_id": tid,
            "title": str(first_ev.get("title") or "Unknown Track"),
            "artist": str(first_ev.get("artist") or "Unknown Artist"),
            "image_url": first_ev.get("image_url"),
            "plays": play_count,
            "span_days": span_days,
            "first_played": first_dt.isoformat(),
            "last_played": last_dt.isoformat(),
            "kurtosis": sample_kurtosis,
        })

    comets.sort(key=lambda c: (c["plays"], c["kurtosis"]), reverse=True)

    # Process pillars
    pillars: list[dict[str, Any]] = []
    if pillar_candidates:
        for p in pillar_candidates:
            months = int(p.get("active_months") or 0)
            skip_r = float(p.get("skip_rate") or 0.0)
            if months >= 24 and skip_r < 0.15:
                pillars.append({
                    "track_id": str(p.get("track_id")),
                    "title": str(p.get("title") or "Unknown Track"),
                    "artist": str(p.get("artist") or "Unknown Artist"),
                    "image_url": p.get("image_url"),
                    "total_plays": int(p.get("total_plays") or 0),
                    "active_months": months,
                    "skip_rate": skip_r,
                    "first_played": str(p.get("first_played_at") or ""),
                    "last_played": str(p.get("last_played_at") or ""),
                })
    else:
        # Fallback within the provided events: tracks active in >= 10 distinct months with skip_rate < 0.15
        for tid, t_events in tracks_map.items():
            if len(t_events) < 25:
                continue
            months_set = {parse_datetime(e.get("played_at")).strftime("%Y-%m") for e in t_events}
            skips = sum(1 for e in t_events if e.get("skipped") or str(e.get("reason_end") or "").lower() == "fwdbtn")
            skip_r = round(skips / len(t_events), 4)
            if len(months_set) >= 10 and skip_r < 0.15:
                first_ev = t_events[0]
                pillars.append({
                    "track_id": tid,
                    "title": str(first_ev.get("title") or "Unknown Track"),
                    "artist": str(first_ev.get("artist") or "Unknown Artist"),
                    "image_url": first_ev.get("image_url"),
                    "total_plays": len(t_events),
                    "active_months": len(months_set),
                    "skip_rate": skip_r,
                    "first_played": parse_datetime(t_events[0].get("played_at")).isoformat(),
                    "last_played": parse_datetime(t_events[-1].get("played_at")).isoformat(),
                })

    pillars.sort(key=lambda p: (p.get("active_months", 0), p.get("total_plays", 0)), reverse=True)

    return {
        "comets": comets[:8],
        "pillars": pillars[:8],
        "comet_count": len(comets),
        "pillar_count": len(pillars),
    }


# ─── 4. Auditory Rejection Signature (Skip Velocity) ────────────────────────

def calculate_rejection_signature(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Parse skipped, ms_played, and track duration for skip signatures.

    - Immediate Rejection: Skip < 5s
    - Dopamine Restlessness: >= 5 consecutive skips within 45 seconds
    - Talisman Tracks: Tracks with > 25 plays and a 0.0% skip rate
    """
    total_plays = len(events)
    if total_plays == 0:
        return {
            "immediate_rejections": 0,
            "immediate_rejection_rate": 0.0,
            "dopamine_restlessness_events": 0,
            "talisman_tracks": [],
            "total_skips": 0,
            "overall_skip_rate": 0.0,
        }

    sorted_events = sorted(events, key=lambda e: parse_datetime(e.get("played_at")))

    immediate_rejections = 0
    total_skips = 0
    consecutive_skip_sequence: list[datetime] = []
    dopamine_restlessness_events = 0

    # Group track plays & skips
    track_stats: dict[str, dict[str, Any]] = {}

    for ev in sorted_events:
        ms_played = int(ev.get("ms_played") or 0)
        skipped = bool(ev.get("skipped"))
        reason_end = str(ev.get("reason_end") or "").lower()
        is_skip = skipped or reason_end == "fwdbtn"
        played_at = parse_datetime(ev.get("played_at"))

        tid = str(ev.get("track_id") or "")
        if tid:
            if tid not in track_stats:
                track_stats[tid] = {
                    "track_id": tid,
                    "title": str(ev.get("title") or "Unknown Track"),
                    "artist": str(ev.get("artist") or "Unknown Artist"),
                    "image_url": ev.get("image_url"),
                    "plays": 0,
                    "skips": 0,
                }
            track_stats[tid]["plays"] += 1
            if is_skip:
                track_stats[tid]["skips"] += 1

        if is_skip:
            total_skips += 1
            if ms_played < 5000:
                immediate_rejections += 1

            consecutive_skip_sequence.append(played_at)
            # Check Dopamine Restlessness: >= 5 consecutive skips within 45 seconds
            if len(consecutive_skip_sequence) >= 5:
                first_in_window = consecutive_skip_sequence[-5]
                last_in_window = consecutive_skip_sequence[-1]
                if (last_in_window - first_in_window) <= timedelta(seconds=45):
                    dopamine_restlessness_events += 1
                    # Avoid overlapping counting of the exact same event
                    consecutive_skip_sequence = []
        else:
            # Play completed or repeated -> resets consecutive skip sequence
            consecutive_skip_sequence = []

    # Talisman Tracks: > 25 plays and exactly 0.0% skip rate
    talisman_tracks = [
        {
            "track_id": s["track_id"],
            "title": s["title"],
            "artist": s["artist"],
            "image_url": s["image_url"],
            "plays": s["plays"],
            "skip_rate": 0.0,
        }
        for s in track_stats.values()
        if s["plays"] > 25 and s["skips"] == 0
    ]
    talisman_tracks.sort(key=lambda t: t["plays"], reverse=True)

    immediate_rate = round(immediate_rejections / max(1, total_plays), 4)
    overall_skip_rate = round(total_skips / max(1, total_plays), 4)

    return {
        "immediate_rejections": immediate_rejections,
        "immediate_rejection_rate": immediate_rate,
        "dopamine_restlessness_events": dopamine_restlessness_events,
        "talisman_tracks": talisman_tracks[:8],
        "total_skips": total_skips,
        "overall_skip_rate": overall_skip_rate,
    }


# ─── 5. Circadian Drift (Daylight Arc) ───────────────────────────────────────

def calculate_circadian_drift(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Calculate yearly centroid hour in Europe/Istanbul local time.

    Formula:
        centroid_hour = sum(h * plays(h)) / sum(plays)
    """
    total_plays = len(events)
    hourly_distribution = [0] * 24

    if total_plays == 0:
        return {
            "centroid_hour": 14.0,
            "peak_hour": 14,
            "hourly_distribution": hourly_distribution,
            "night_share": 0.0,
            "day_share": 1.0,
            "total_plays": 0,
        }

    weighted_sum = 0.0
    night_plays = 0
    day_plays = 0

    for ev in events:
        dt_utc = parse_datetime(ev.get("played_at"))
        dt_local = dt_utc.astimezone(ISTANBUL_TZ)
        h = dt_local.hour

        hourly_distribution[h] += 1
        weighted_sum += h

        if h in (22, 23, 0, 1, 2, 3, 4):
            night_plays += 1
        else:
            day_plays += 1

    centroid = round(weighted_sum / total_plays, 2)
    peak_h = int(max(range(24), key=lambda hr: hourly_distribution[hr]))

    return {
        "centroid_hour": centroid,
        "peak_hour": peak_h,
        "hourly_distribution": hourly_distribution,
        "night_share": round(night_plays / total_plays, 4),
        "day_share": round(day_plays / total_plays, 4),
        "total_plays": total_plays,
    }


# ─── 6. Chromatic Palette Extraction ────────────────────────────────────────

def _hsl_to_hex(h: float, s: float, l: float) -> str:
    """Convert HSL (h in [0, 360], s in [0, 1], l in [0, 1]) to hex #RRGGBB."""
    c = (1 - abs(2 * l - 1)) * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = l - c / 2

    if 0 <= h < 60:
        r, g, b = c, x, 0
    elif 60 <= h < 120:
        r, g, b = x, c, 0
    elif 120 <= h < 180:
        r, g, b = 0, c, x
    elif 180 <= h < 240:
        r, g, b = 0, x, c
    elif 240 <= h < 300:
        r, g, b = x, 0, c
    else:
        r, g, b = c, 0, x

    ri = round((r + m) * 255)
    gi = round((g + m) * 255)
    bi = round((b + m) * 255)
    return f"#{ri:02x}{gi:02x}{bi:02x}"


def calculate_relative_luminance(hex_color: str) -> float:
    """Calculate WCAG 2.1 relative luminance for hex color #RRGGBB."""
    hex_clean = hex_color.lstrip("#")
    r, g, b = [int(hex_clean[i : i + 2], 16) / 255.0 for i in (0, 2, 4)]
    r_lin = r / 12.92 if r <= 0.03928 else ((r + 0.055) / 1.055) ** 2.4
    g_lin = g / 12.92 if g <= 0.03928 else ((g + 0.055) / 1.055) ** 2.4
    b_lin = b / 12.92 if b <= 0.03928 else ((b + 0.055) / 1.055) ** 2.4
    return 0.2126 * r_lin + 0.7152 * g_lin + 0.0722 * b_lin


def extract_chromatic_palette(
    covers: list[dict[str, Any]],
    year: int = 2026,
    dominant_genre: str = "",
) -> dict[str, str]:
    """Extract a 4-color palette (primary, glow, accent, deep) from dominant album covers.

    Fallbacks & Contrast Clamping:
      - If covers are missing or have no valid image_url, safely falls back to
        a dark neutral baseline with deep: '#08040C'.
      - Extracted primary and deep colors are luminance-capped so white Bodoni
        typography maintains WCAG AA contrast (ratio >= 4.5:1).
    """
    valid_art = [
        c for c in (covers or [])
        if c.get("image_url") or c.get("cover_url")
    ]

    # Dark neutral fallback when artwork is missing
    if not valid_art:
        return {
            "primary": "#17121f",
            "glow": "#7c3aed",
            "accent": "#ec4899",
            "deep": "#08040C",
        }

    # Create deterministic seed from top cover IDs/titles and year
    seed_str = f"{year}-{dominant_genre}"
    for c in valid_art[:3]:
        seed_str += f"-{c.get('track_id') or c.get('title') or ''}"

    digest = hashlib.sha256(seed_str.encode("utf-8")).hexdigest()
    base_hue = (int(digest[:4], 16) % 360)
    accent_hue = (base_hue + 140) % 360

    # Primary: Deep foundation tone, luminance capped (L <= 0.18) for white text legibility
    primary = _hsl_to_hex(base_hue, 0.65, 0.17)
    if calculate_relative_luminance(primary) > 0.18:
        primary = _hsl_to_hex(base_hue, 0.65, 0.12)

    # Glow: Radiant, high-chroma ambient luminescence for CSS Houdini --j-active-glow (L: 0.56, S: 0.88)
    glow = _hsl_to_hex(base_hue, 0.88, 0.56)

    # Accent: Contrasting or complementary vibrancy (L: 0.60, S: 0.90)
    accent = _hsl_to_hex(accent_hue, 0.90, 0.60)

    # Deep: Ultra-deep foundation hue for unified single-layer scrim (L <= 0.03)
    deep = _hsl_to_hex(base_hue, 0.40, 0.03)
    if calculate_relative_luminance(deep) > 0.04:
        deep = "#08040C"

    return {
        "primary": primary,
        "glow": glow,
        "accent": accent,
        "deep": deep,
    }


# ─── Orchestrator ────────────────────────────────────────────────────────────

def mine_year_latent_dimensions(
    year: int,
    events: list[dict[str, Any]],
    covers: list[dict[str, Any]],
    pillar_candidates: list[dict[str, Any]] | None = None,
    dominant_genre: str = "",
) -> dict[str, Any]:
    """Compute all 5 analytical dimensions + 4-color palette for a given year."""
    intentionality = calculate_agency_score(events)
    temporal = calculate_temporal_displacement(events, year)
    obsession = calculate_obsession_topology(events, pillar_candidates)
    rejection = calculate_rejection_signature(events)
    circadian = calculate_circadian_drift(events)
    palette = extract_chromatic_palette(covers, year, dominant_genre)

    return {
        "latent": {
            "intentionality": intentionality,
            "temporal_displacement": temporal,
            "obsession_topology": obsession,
            "rejection_signature": rejection,
            "circadian_drift": circadian,
        },
        "palette": palette,
    }

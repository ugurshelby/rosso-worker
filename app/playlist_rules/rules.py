"""
Auto-playlist rule engine (plan §7.1).

Each rule type queries play_events from Supabase and returns a ranked
list of track IDs for a given user + parameters. Rules always exclude
incognito events (incognito_mode = false).

Rule types:
- top_month(user_id, year, month, n, sort_by) → top N tracks for a month
- top_year(user_id, year, n, sort_by)         → top N tracks for a year
- morning_routine(user_id, n)          → tracks most played 06:00–09:00
- nostalgia(user_id, year, n)          → tracks first played in a given year
- most_skipped(user_id, n)             → most frequently skipped tracks
- obsession(user_id, n)                → tracks played 10+ times in last 30d
"""
from __future__ import annotations

import calendar
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from supabase import create_client, Client

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

logger = logging.getLogger(__name__)


def _db() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)


@dataclass
class RuleTrack:
    """A track returned by a rule query."""
    track_id: str
    raw_track_name: str
    raw_artist_name: str
    play_count: int
    score: float  # rule-specific relevance score


def _fetch_top(
    *,
    user_id: str,
    n: int,
    from_iso: str,
    to_iso: str,
    only_skipped: bool = False,
    min_plays: Optional[int] = None,
    hour_from: Optional[int] = None,
    hour_to: Optional[int] = None,
    sort_by: str = "plays",
) -> list[RuleTrack]:
    """
    Common query: aggregate play_events for user in [from_iso, to_iso],
    apply optional filters, return top n tracks.

    `sort_by` ('plays' | 'duration') kullanıcının kuralda seçtiği sıralama
    ölçütüdür. Tanınmayan bir değer gelirse 'plays'e düşeriz: kolonda CHECK
    var ama worker DB'ye tek güvenen taraf değil — bilinmeyen bir değeri
    olduğu gibi göndermek RPC'yi sessizce çalma sayısına düşürürdü.
    """
    if sort_by not in ("plays", "duration"):
        logger.warning("Bilinmeyen sort_by=%r, 'plays' kullanılıyor", sort_by)
        sort_by = "plays"
    db = _db()
    query = (
        db.rpc(
            "get_top_tracks_for_rule",
            {
                "p_user_id": user_id,
                "p_from": from_iso,
                "p_to": to_iso,
                "p_skipped": only_skipped,
                "p_min_plays": min_plays,
                "p_hour_from": hour_from,
                "p_hour_to": hour_to,
                "p_limit": n,
                "p_sort_by": sort_by,
            },
        )
        .execute()
    )
    rows = query.data or []
    return [
        RuleTrack(
            track_id=row["track_id"] or "",
            raw_track_name=row.get("raw_track_name") or "",
            raw_artist_name=row.get("raw_artist_name") or "",
            play_count=row.get("play_count", 0),
            # score = sıralamanın dayandığı büyüklük. Süre ölçütünde bunu
            # çalma sayısı bırakmak, listeyi score'a göre yeniden sıralayan
            # her tüketiciye RPC'nin verdiğinden BAŞKA bir sıra verirdi.
            score=float(
                (row.get("total_ms") or 0)
                if sort_by == "duration"
                else row.get("play_count", 0)
            ),
        )
        for row in rows
        if row.get("track_id")
    ]


def top_month(
    user_id: str, year: int, month: int, n: int = 50, sort_by: str = "plays"
) -> list[RuleTrack]:
    """Top N tracks for a specific calendar month."""
    _, last_day = calendar.monthrange(year, month)
    from_iso = f"{year:04d}-{month:02d}-01T00:00:00+00:00"
    to_iso = f"{year:04d}-{month:02d}-{last_day:02d}T23:59:59+00:00"
    return _fetch_top(
        user_id=user_id, n=n, from_iso=from_iso, to_iso=to_iso, sort_by=sort_by
    )


def top_year(
    user_id: str, year: int, n: int = 50, sort_by: str = "plays"
) -> list[RuleTrack]:
    """Top N tracks for a full calendar year."""
    from_iso = f"{year:04d}-01-01T00:00:00+00:00"
    to_iso = f"{year:04d}-12-31T23:59:59+00:00"
    return _fetch_top(
        user_id=user_id, n=n, from_iso=from_iso, to_iso=to_iso, sort_by=sort_by
    )


def morning_routine(user_id: str, n: int = 50) -> list[RuleTrack]:
    """Tracks most played between 06:00–09:00 (all time)."""
    now = datetime.now(timezone.utc)
    from_iso = "2000-01-01T00:00:00+00:00"
    to_iso = now.isoformat()
    return _fetch_top(
        user_id=user_id,
        n=n,
        from_iso=from_iso,
        to_iso=to_iso,
        hour_from=6,
        hour_to=9,
    )


def nostalgia(user_id: str, year: int, n: int = 50) -> list[RuleTrack]:
    """Tracks first heard in a specific year (by min played_at)."""
    from_iso = f"{year:04d}-01-01T00:00:00+00:00"
    to_iso = f"{year:04d}-12-31T23:59:59+00:00"
    return _fetch_top(user_id=user_id, n=n, from_iso=from_iso, to_iso=to_iso)


def most_skipped(user_id: str, n: int = 50) -> list[RuleTrack]:
    """Most frequently skipped tracks (all time)."""
    now = datetime.now(timezone.utc)
    from_iso = "2000-01-01T00:00:00+00:00"
    to_iso = now.isoformat()
    return _fetch_top(
        user_id=user_id,
        n=n,
        from_iso=from_iso,
        to_iso=to_iso,
        only_skipped=True,
    )


def obsession(user_id: str, n: int = 50) -> list[RuleTrack]:
    """Tracks played 10+ times in the last 30 days."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    from_dt = now - timedelta(days=30)
    return _fetch_top(
        user_id=user_id,
        n=n,
        from_iso=from_dt.isoformat(),
        to_iso=now.isoformat(),
        min_plays=10,
    )

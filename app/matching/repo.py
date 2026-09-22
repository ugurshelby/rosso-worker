"""Supabase tabanlı TrackRepo — matcher'a gerçek tracks erişimi sağlar.

Batch matching için: önce spotify_id/ISRC toplu çekilir (az sorgu),
fuzzy gerekince pg_trgm ile aday alınır.

In-memory cache: aynı job içinde tekrar görülen spotify_id ve ISRC
değerleri için DB'ye gidilmez. 120K satırlık exportlarda ~15dk → ~1dk.
"""
from __future__ import annotations

from typing import Any

from supabase import Client

_MISS = object()  # "not found" sentinel — None ile karıştırılmaz


class SupabaseTrackRepo:
    """matcher.TrackRepo protokolünü Supabase ile karşılar."""

    def __init__(self, client: Client):
        self._client = client
        # job süresince geçerli in-memory cache'ler
        self._spotify_cache: dict[str, dict[str, Any] | object] = {}
        self._isrc_cache: dict[str, dict[str, Any] | object] = {}

    def find_by_spotify_id(self, spotify_id: str) -> dict[str, Any] | None:
        if spotify_id in self._spotify_cache:
            hit = self._spotify_cache[spotify_id]
            return None if hit is _MISS else hit  # type: ignore[return-value]

        res = (
            self._client.table("tracks")
            .select("id, title, artists, duration_ms")
            .eq("spotify_id", spotify_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            rows = self._find_by_alias(spotify_id)
        result: dict[str, Any] | object = rows[0] if rows else _MISS
        self._spotify_cache[spotify_id] = result
        return None if result is _MISS else result  # type: ignore[return-value]

    def _find_by_alias(self, spotify_id: str) -> list[dict[str, Any]]:
        """Birleştirilmiş eski ID → kanonik şarkı (0335, 2026-09-22).

        ISRC birleştirmesi "B aslında A'dır" notunu `track_spotify_alias`'a
        yazar. Recently-played ISRC vermediği için not olmadan B her gelişinde
        yeni bir şarkı olarak açılır, dinlemeler yine bölünürdü.
        """
        alias = (
            self._client.table("track_spotify_alias")
            .select("track_id")
            .eq("spotify_id", spotify_id)
            .limit(1)
            .execute()
        )
        alias_rows = alias.data or []
        if not alias_rows:
            return []
        res = (
            self._client.table("tracks")
            .select("id, title, artists, duration_ms")
            .eq("id", alias_rows[0]["track_id"])
            .limit(1)
            .execute()
        )
        return res.data or []

    def find_by_isrc(self, isrc: str) -> dict[str, Any] | None:
        if isrc in self._isrc_cache:
            hit = self._isrc_cache[isrc]
            return None if hit is _MISS else hit  # type: ignore[return-value]

        res = (
            self._client.table("tracks")
            .select("id, title, artists, duration_ms")
            .eq("isrc", isrc)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        result: dict[str, Any] | object = rows[0] if rows else _MISS
        self._isrc_cache[isrc] = result
        return None if result is _MISS else result  # type: ignore[return-value]

    def find_fuzzy_candidates(
        self, title: str, artists: list[str]
    ) -> list[dict[str, Any]]:
        # pg_trgm benzerliğiyle başlık adayları (ilk 10) — fuzzy sorgular cache'lenmez
        res = (
            self._client.table("tracks")
            .select("id, title, artists, duration_ms")
            .ilike("title", f"%{title[:40]}%")
            .limit(10)
            .execute()
        )
        return res.data or []

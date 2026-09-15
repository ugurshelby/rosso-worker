"""Worker yapılandırması — env'den okunur, eksikse erken patlar."""
from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv
from pydantic import BaseModel

# worker/.env veya kök .env.local'den yükle (varsa)
load_dotenv()
load_dotenv("../.env.local")


class Settings(BaseModel):
    """Worker server-side; service role kullanır (RLS bypass ile batch insert)."""

    supabase_url: str
    supabase_service_role_key: str

    # Storage bucket — Spotify ZIP'leri (migration 0009 ile uyumlu)
    export_bucket: str = "spotify-exports"
    # play_events batch insert boyutu
    batch_size: int = 1000

    # Spotify API (G.2 track lookup)
    spotify_client_id: str = ""
    spotify_client_secret: str = ""

    # YT Music OAuth (Faz 5)
    youtube_client_id: str = ""
    youtube_client_secret: str = ""

    # Token şifreleme (Faz 5) — Python worker token'ları çözmek için
    token_encryption_key: str = ""

    # Genre enrichment: Deezer (birincil) + Last.fm (fallback) — spec 2026-06-30.
    # Spotify Dev Mode kotası (23 saat 429) genre için kullanılamadığından terk edildi.
    # Last.fm key TR coverage için kritik (Deezer TR albümlerde sık boş döner).
    lastfm_api_key: str = ""


@lru_cache
def get_settings() -> Settings:
    url = os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL ve SUPABASE_SERVICE_ROLE_KEY zorunlu (worker server-side)."
        )
    return Settings(
        supabase_url=url,
        supabase_service_role_key=key,
        spotify_client_id=os.environ.get("SPOTIFY_CLIENT_ID", ""),
        spotify_client_secret=os.environ.get("SPOTIFY_CLIENT_SECRET", ""),
        youtube_client_id=os.environ.get("YOUTUBE_CLIENT_ID", ""),
        youtube_client_secret=os.environ.get("YOUTUBE_CLIENT_SECRET", ""),
        token_encryption_key=os.environ.get("TOKEN_ENCRYPTION_KEY", ""),
        # İki isim de desteklenir: LAST_FM_API_KEY (.env.local standardı) ya da LASTFM_API_KEY.
        lastfm_api_key=os.environ.get("LAST_FM_API_KEY") or os.environ.get("LASTFM_API_KEY", ""),
    )

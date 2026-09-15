"""Genre pipeline özel hataları."""
from __future__ import annotations


class RateLimitError(Exception):
    """Bir genre kaynağı 429 döndürdü. Provider + Retry-After taşır.

    build_track_genre_data bunu yakalayıp batch'e taşır; run_one_genre_batch
    set_cooldown besler. Track lookup_failed YAZILMAZ (429 kalıcı değil).
    """

    def __init__(self, provider: str, retry_after: float | None = None) -> None:
        super().__init__(f"rate limited: {provider}")
        self.provider = provider
        self.retry_after = retry_after

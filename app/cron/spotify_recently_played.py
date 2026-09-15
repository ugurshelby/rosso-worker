"""Spotify recently-played sync cron — saatlik, cooldown bak, sync et, çık.
Railway: python -m app.cron.spotify_recently_played."""
from __future__ import annotations

import logging
import sys

import httpx

from app.cron._logging import setup_logging

setup_logging()
logger = logging.getLogger("rosso.worker.cron.spotify_recently_played")


def main() -> int:
    from app.config import get_settings
    from app.db import get_client
    from app.pipeline.recently_played_runner import run_one_recently_played_sync
    from app.services import run_log

    client = get_client()
    settings = get_settings()
    crypto_key = getattr(settings, "token_encryption_key", "") or ""

    try:
        with httpx.Client(timeout=15) as http:
            result = run_one_recently_played_sync(client, http, crypto_key)
        logger.info(
            '{"run":"spotify_recently_played","status":"%s","users":%d,"events":%d,"errors":%d}',
            result["outcome"], result.get("users_processed", 0), result.get("events_written", 0),
            result.get("errors", 0),
        )
        run_log.record_run(client, "spotify_recently_played", outcome=result["outcome"], stats=result)
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("Spotify recently-played sync başarısız")
        run_log.record_run(client, "spotify_recently_played", outcome="error", error=str(exc)[:500])
        return 1


if __name__ == "__main__":
    sys.exit(main())

"""Playlist tazeleme cron — günde 1-2 kez, snapshot_id diff ile senkron.
Railway: python -m app.cron.playlist_refresh."""
from __future__ import annotations

import logging
import sys

import httpx

from app.cron._logging import setup_logging

setup_logging()
logger = logging.getLogger("rosso.worker.cron.playlist_refresh")


def main() -> int:
    from app.config import get_settings
    from app.db import get_client
    from app.pipeline.playlist_refresh_runner import run_one_playlist_refresh
    from app.services import run_log

    client = get_client()
    settings = get_settings()
    crypto_key = getattr(settings, "token_encryption_key", "") or ""

    try:
        with httpx.Client(timeout=15) as http:
            result = run_one_playlist_refresh(client, http, crypto_key)
        logger.info(
            '{"run":"playlist_refresh","status":"%s","playlists_updated":%d,"errors":%d,"skipped":%d}',
            result["outcome"], result.get("playlists_updated", 0),
            result.get("errors", 0), result.get("skipped_playlists", 0),
        )
        run_log.record_run(client, "playlist_refresh", outcome=result["outcome"], stats=result)
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("Playlist tazeleme başarısız")
        run_log.record_run(client, "playlist_refresh", outcome="error", error=str(exc)[:500])
        return 1


if __name__ == "__main__":
    sys.exit(main())

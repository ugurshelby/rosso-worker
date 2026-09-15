"""Spotify lookup servisi testleri — G.2/G.3 (I/O'suz)."""
from unittest.mock import MagicMock, patch
import pytest

from app.services.spotify_lookup import (
    upsert_track,
    _with_retry,
)


# ─── _with_retry ─────────────────────────────────────────────────────────────

def test_retry_succeeds_on_first_attempt():
    fn = MagicMock(return_value="ok")
    result = _with_retry(fn)
    assert result == "ok"
    fn.assert_called_once()


def test_retry_raises_non_retriable_immediately():
    import httpx
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.headers = {}
    err = httpx.HTTPStatusError("not found", request=MagicMock(), response=mock_resp)
    fn = MagicMock(side_effect=err)
    with pytest.raises(httpx.HTTPStatusError):
        _with_retry(fn)
    assert fn.call_count == 1  # 404 → retry değil


def test_retry_exhausts_on_generic_error():
    import os
    fn = MagicMock(side_effect=ConnectionError("timeout"))
    with patch("app.services.spotify_lookup._MAX_RETRIES", 2), \
         patch("app.services.spotify_lookup._RETRY_BASE_DELAY", 0.0), \
         patch("time.sleep"):
        with pytest.raises(ConnectionError):
            _with_retry(fn)
    assert fn.call_count == 2


def test_retry_after_over_cap_aborts_without_sleeping():
    """429 Retry-After cap'i aşıyorsa worker SAATLERCE sleep etmemeli.

    Gerçek olay (2026-06-22): Spotify Retry-After=70771 (~19.6 saat) döndürdü,
    worker time.sleep(70771) ile saatlerce bloke oldu → tüm job asılı kaldı.
    Doğru davranış: cap aşılırsa hiç sleep yapmadan SpotifyQuotaExhausted fırlatır
    (circuit-breaker sinyali) → çağıran o job için lookup'ı bırakır
    (CLAUDE.md §2 servis izolasyonu, §3 event silinmez).
    """
    import httpx
    from app.services.spotify_lookup import SpotifyQuotaExhausted

    mock_resp = MagicMock()
    mock_resp.status_code = 429
    mock_resp.headers = {"Retry-After": "70771"}  # ~19.6 saat
    err = httpx.HTTPStatusError("rate limited", request=MagicMock(), response=mock_resp)
    fn = MagicMock(side_effect=err)

    with patch("app.services.spotify_lookup._MAX_RETRIES", 3), \
         patch("app.services.spotify_lookup._RETRY_BASE_DELAY", 0.0), \
         patch("time.sleep") as mock_sleep:
        with pytest.raises(SpotifyQuotaExhausted):
            _with_retry(fn)

    # Kritik: cap'i aşan Retry-After için HİÇ sleep yapılmamalı (saatlerce bloke yok)
    mock_sleep.assert_not_called()
    # Ve absürt bekleme için boşuna tekrar denenmemeli
    assert fn.call_count == 1


def test_retry_after_within_cap_sleeps_capped_value():
    """429 Retry-After cap içindeyse normal retry: bekle ve tekrar dene."""
    import httpx
    from app.services.spotify_lookup import _MAX_RETRY_AFTER

    mock_resp = MagicMock()
    mock_resp.status_code = 429
    mock_resp.headers = {"Retry-After": "5"}  # cap (60) altında
    err = httpx.HTTPStatusError("rate limited", request=MagicMock(), response=mock_resp)
    # İlk 429, sonra başarı
    fn = MagicMock(side_effect=[err, "ok"])

    with patch("app.services.spotify_lookup._MAX_RETRIES", 3), \
         patch("app.services.spotify_lookup._RETRY_BASE_DELAY", 0.0), \
         patch("time.sleep") as mock_sleep:
        result = _with_retry(fn)

    assert result == "ok"
    slept = [call.args[0] for call in mock_sleep.call_args_list]
    assert slept == [5.0]
    assert all(s <= _MAX_RETRY_AFTER for s in slept)


# ─── upsert_track ────────────────────────────────────────────────────────────

def test_upsert_track_returns_id():
    client = MagicMock()
    mock_res = MagicMock()
    mock_res.data = [{"id": "track-uuid-1"}]
    (
        client.table.return_value
        .upsert.return_value
        .execute.return_value
    ) = mock_res

    track_data = {
        "spotify_id": "abc123",
        "isrc": "USRC1700609",
        "title": "Test Track",
        "artists": ["Artist A"],
        "duration_ms": 180000,
        "album": "Test Album",
    }
    result = upsert_track(track_data, client)
    assert result == "track-uuid-1"


def test_upsert_track_returns_none_on_error():
    client = MagicMock()
    client.table.return_value.upsert.return_value.execute.side_effect = Exception("DB error")
    track_data = {"spotify_id": "abc", "title": "T", "artists": []}
    result = upsert_track(track_data, client)
    assert result is None



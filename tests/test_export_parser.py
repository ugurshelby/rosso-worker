"""Spotify export parser testleri — spotify-export-research.md §1-§7."""
from app.parser.spotify_export import (
    classify_event,
    is_valid_play,
    deduplicate,
    strip_sensitive,
    normalize_event,
    sort_events_chronologically,
)


# ─── classify_event (research §5: öncelik audiobook→podcast→music→unknown) ───

def test_classify_music():
    raw = {"master_metadata_track_name": "Bohemian Rhapsody"}
    assert classify_event(raw) == "music"


def test_classify_podcast():
    raw = {"master_metadata_track_name": None, "episode_name": "Ep 1"}
    assert classify_event(raw) == "podcast"


def test_classify_audiobook_takes_priority():
    # audiobook_title öncelikli — episode/track dolu olsa bile
    raw = {
        "audiobook_title": "Sapiens",
        "episode_name": "x",
        "master_metadata_track_name": "y",
    }
    assert classify_event(raw) == "audiobook"


def test_classify_unknown():
    raw = {"master_metadata_track_name": None}
    assert classify_event(raw) == "unknown"


# ─── is_valid_play (research §2) ───

def test_invalid_zero_ms():
    assert is_valid_play({"ms_played": 0}) is False


def test_invalid_under_5s():
    assert is_valid_play({"ms_played": 4999}) is False


def test_valid_over_5s():
    assert is_valid_play({"ms_played": 5000}) is True


def test_valid_skipped_over_5s_is_kept():
    # research §2: >=5000 + skipped → SAKLA (skip istatistiği için)
    assert is_valid_play({"ms_played": 6744, "skipped": True}) is True


# ─── deduplicate (research §4: aynı ts+uri at, farklı uri KORU) ───

def test_dedup_same_ts_same_uri_removed():
    events = [
        {"ts": "2023-08-27T08:52:41Z", "spotify_track_uri": "spotify:track:A", "ms_played": 6000},
        {"ts": "2023-08-27T08:52:41Z", "spotify_track_uri": "spotify:track:A", "ms_played": 6000},
    ]
    assert len(deduplicate(events)) == 1


def test_dedup_same_ts_different_uri_kept():
    # research §4: aynı ts + FARKLI uri = farklı kayıt → ikisi de korunur
    events = [
        {"ts": "2023-08-27T08:52:41Z", "spotify_track_uri": "spotify:track:A", "ms_played": 6000},
        {"ts": "2023-08-27T08:52:41Z", "spotify_track_uri": "spotify:track:B", "ms_played": 7000},
    ]
    assert len(deduplicate(events)) == 2


def test_dedup_podcast_uri_key():
    events = [
        {"ts": "2023-01-01T00:00:00Z", "spotify_episode_uri": "spotify:episode:E", "ms_played": 9000},
        {"ts": "2023-01-01T00:00:00Z", "spotify_episode_uri": "spotify:episode:E", "ms_played": 9000},
    ]
    assert len(deduplicate(events)) == 1


# ─── strip_sensitive (kritik: ip_addr çıktıda YOK) ───

def test_strip_removes_ip_addr():
    raw = {"ms_played": 6000, "ip_addr": "1.2.3.4", "conn_country": "TR"}
    out = strip_sensitive(raw)
    assert "ip_addr" not in out
    # ip_addr'in eski adı da temizlenmeli
    raw2 = {"ms_played": 6000, "ip_addr_decrypted": "1.2.3.4"}
    assert "ip_addr_decrypted" not in strip_sensitive(raw2)


def test_strip_keeps_conn_country():
    out = strip_sensitive({"ms_played": 6000, "conn_country": "TR"})
    assert out["conn_country"] == "TR"


# ─── normalize_event (DB satırına dönüştür; reason/platform TEXT korunur) ───

def test_normalize_music_event():
    raw = {
        "ts": "2023-08-27T08:52:41Z",
        "master_metadata_track_name": "Song",
        "master_metadata_album_artist_name": "Artist",
        "spotify_track_uri": "spotify:track:A",
        "ms_played": 6000,
        "reason_start": "trackdone",
        "reason_end": "fwdbtn",
        "platform": "android",
        "skipped": True,
        "conn_country": "TR",
        "ip_addr": "1.2.3.4",
    }
    ev = normalize_event(raw, source="spotify_export")
    assert ev["content_type"] == "music"
    assert ev["played_at"] == "2023-08-27T08:52:41Z"
    assert ev["raw_track_name"] == "Song"
    assert ev["raw_artist_name"] == "Artist"
    assert ev["spotify_track_uri"] == "spotify:track:A"
    assert ev["ms_played"] == 6000
    assert ev["reason_end"] == "fwdbtn"  # TEXT korunur, enum yok
    assert ev["platform"] == "spotify"   # kaynak platform (cihaz değil)
    assert ev["source"] == "spotify_export"
    assert ev["skipped"] is True
    assert "ip_addr" not in ev


# ─── sort_events_chronologically (research §1: ts'e göre, dosya adına değil) ───

def test_sort_by_ts():
    events = [
        {"ts": "2023-10-01T00:00:00Z"},
        {"ts": "2023-05-01T00:00:00Z"},
        {"ts": "2023-08-01T00:00:00Z"},
    ]
    out = sort_events_chronologically(events)
    assert [e["ts"] for e in out] == [
        "2023-05-01T00:00:00Z",
        "2023-08-01T00:00:00Z",
        "2023-10-01T00:00:00Z",
    ]

"""Technical Log parser testleri."""
from app.parser.technical_log_parser import (
    classify_technical_entry,
    parse_car_detection_events,
    parse_collection_events,
    parse_daylist,
    parse_home_section,
    parse_playlist_track_events,
)
from tests.fixtures.techlog_samples import (
    ADD_TO_PLAYLIST,
    ADDED_TO_COLLECTION,
    ADDED_TO_PLAYLIST,
    CAR_DETECTION,
    DAYLIST_GENERATED,
    HOME_SECTION,
    REMOVED_FROM_COLLECTION,
)


# ─── classify_technical_entry ────────────────────────────────────────────────

def test_classify_hard_discard():
    assert classify_technical_entry("DeviceIdentifier.json") == "hard_discard"


def test_classify_known_files():
    assert classify_technical_entry("AddedToCollection.json") == "added_to_collection"
    assert classify_technical_entry("RemovedFromCollection.json") == "removed_from_collection"
    assert classify_technical_entry("AddedToPlaylist.json") == "added_to_playlist"
    assert classify_technical_entry("AddToPlaylist.json") == "added_to_playlist"
    assert classify_technical_entry("PlaylistCreated.json") == "playlist_created"
    assert classify_technical_entry("DaylistGenerated.json") == "daylist_generated"
    assert classify_technical_entry("OnRepeatContents.json") == "on_repeat"
    assert classify_technical_entry("CarDetectionEvent.json") == "car_detection"
    assert classify_technical_entry("HomeSectionResponse.json") == "home_section"


def test_classify_shuffle_sequence_prefix():
    assert classify_technical_entry("ShuffleSequenceEvent.json") == "shuffle_sequence"
    assert classify_technical_entry("ShuffleSequenceEvent_0.json") == "shuffle_sequence"
    assert classify_technical_entry("ShuffleSequenceEvent123.json") == "shuffle_sequence"


def test_classify_skip():
    assert classify_technical_entry("CacheEvent.json") == "skip"
    assert classify_technical_entry("AudioEngine.json") == "skip"
    assert classify_technical_entry("BasslineRequests.json") == "skip"


# ─── parse_collection_events ─────────────────────────────────────────────────

def test_added_to_collection_gercek_format():
    """Gerçek format: message_item_uri + timestamp_utc."""
    rows = parse_collection_events(ADDED_TO_COLLECTION, event_type="liked")
    assert len(rows) == 2
    assert rows[0]["spotify_uri"] == "spotify:track:0GabRJwqSIxLv3A139Lu6b"
    assert rows[0]["occurred_at"] == "2026-05-21T06:47:47.718Z"
    assert rows[0]["event_type"] == "liked"


def test_removed_yalniz_track_album_playlist_atlanir():
    """3 kayıttan yalnızca 1'i spotify:track: (playlist + album atlanır)."""
    rows = parse_collection_events(REMOVED_FROM_COLLECTION, event_type="unliked")
    assert len(rows) == 1
    assert rows[0]["spotify_uri"] == "spotify:track:6IerS2EZLcEDn1OetmcDM6"
    assert rows[0]["event_type"] == "unliked"


def test_parse_collection_missing_timestamp_skipped():
    data = [{"message_item_uri": "spotify:track:AAA"}]
    rows = parse_collection_events(data, event_type="liked")
    assert len(rows) == 0


def test_parse_collection_empty():
    assert parse_collection_events([], event_type="liked") == []


def test_parse_collection_non_track_uri_skipped():
    data = [{"message_item_uri": "spotify:album:XYZ", "timestamp_utc": "2024-01-01T00:00:00Z"}]
    rows = parse_collection_events(data, event_type="liked")
    assert len(rows) == 0


# ─── parse_playlist_track_events ─────────────────────────────────────────────

def test_added_to_playlist_tekil_uri():
    """AddedToPlaylist: tekil message_item_uri + message_playlist_uri."""
    rows = parse_playlist_track_events(ADDED_TO_PLAYLIST)
    assert len(rows) == 1
    assert rows[0]["track_uri"] == "spotify:track:4hwZzYOnYRmYbMfWcxCivZ"
    assert rows[0]["playlist_uri"] == "spotify:playlist:1Dx7G5XUqCybOHZzPrGGC1"
    assert rows[0]["added_at"] == "2026-06-06T16:30:26.587Z"


def test_add_to_playlist_cogul_uris():
    """AddToPlaylist: çoğul message_item_uris[]."""
    rows = parse_playlist_track_events(ADD_TO_PLAYLIST)
    assert len(rows) == 1
    assert rows[0]["track_uri"] == "spotify:track:5TTGoX70AFrTvuEtqHK37S"
    assert rows[0]["playlist_uri"] == "spotify:playlist:0JvOz8ztPJm5n6tqUTrruy"


def test_parse_playlist_track_missing_fields_skipped():
    data = [
        {"message_item_uri": "spotify:track:T1"},  # playlist_uri yok → atlanır
        {"message_playlist_uri": "spotify:playlist:P1"},  # item_uri yok → atlanır
    ]
    rows = parse_playlist_track_events(data)
    assert len(rows) == 0


# ─── parse_car_detection_events ──────────────────────────────────────────────

def test_car_detection_seans_eslestirme():
    """Gerçek format: message_is_car_connected + timestamp_utc."""
    sessions = parse_car_detection_events(CAR_DETECTION)
    assert len(sessions) == 1
    s = sessions[0]
    assert s["connected_at"] == "2026-04-23T11:21:44.732Z"
    assert s["disconnected_at"] == "2026-04-23T20:03:34.137Z"
    assert s["duration_seconds"] == 31309  # ~8.7 saat


def test_parse_car_unmatched_connect():
    data = [
        {"timestamp_utc": "2024-03-10T08:00:00Z", "message_is_car_connected": True},
        # disconnect yok
    ]
    sessions = parse_car_detection_events(data)
    assert len(sessions) == 1
    assert sessions[0]["disconnected_at"] is None
    assert sessions[0]["duration_seconds"] is None


def test_parse_car_multiple_sessions():
    data = [
        {"timestamp_utc": "2024-03-10T08:00:00Z", "message_is_car_connected": True},
        {"timestamp_utc": "2024-03-10T08:30:00Z", "message_is_car_connected": False},
        {"timestamp_utc": "2024-03-10T17:00:00Z", "message_is_car_connected": True},
        {"timestamp_utc": "2024-03-10T18:00:00Z", "message_is_car_connected": False},
    ]
    sessions = parse_car_detection_events(data)
    assert len(sessions) == 2
    assert sessions[0]["duration_seconds"] == 1800  # 30 dakika
    assert sessions[1]["duration_seconds"] == 3600  # 1 saat


def test_parse_car_empty():
    assert parse_car_detection_events([]) == []


def test_parse_car_non_list():
    assert parse_car_detection_events("bad_data") == []


# ─── parse_daylist ───────────────────────────────────────────────────────────

def test_daylist_baslik_kelime_frekansi():
    """Gerçek format: message_playlist_title + message_daypart."""
    sig = parse_daylist(DAYLIST_GENERATED)
    assert sig["raw_count"] == 1
    # "angst rock-ish thursday morning" → kelimeler title_word_frequency'de
    assert "angst" in sig["title_word_frequency"]
    assert "thursday" in sig["title_word_frequency"]
    # message_daypart="morning" → time_of_day_distribution'a yansır
    assert sig["time_of_day_distribution"].get("morning") == 1


def test_parse_daylist_empty():
    result = parse_daylist([])
    assert result["raw_count"] == 0


# ─── parse_home_section ──────────────────────────────────────────────────────

def test_home_section_gercek_format():
    """Gerçek format: message_title."""
    sig = parse_home_section(HOME_SECTION)
    assert "Made for you" in sig["titles"]
    assert sig["count"] == 1


def test_parse_home_section_titles():
    data = [
        {"message_title": "Pop but not"},
        {"message_title": "Local alternative"},
        {"message_title": "Pop but not"},  # duplicate → tek olmalı
    ]
    result = parse_home_section(data)
    assert result["count"] == 3
    assert len(result["titles"]) == 2  # dedup
    assert "Pop but not" in result["titles"]
    assert "Local alternative" in result["titles"]


def test_parse_home_section_empty():
    result = parse_home_section([])
    assert result["titles"] == []
    assert result["count"] == 0

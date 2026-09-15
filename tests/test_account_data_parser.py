"""Account Data parser testleri."""
from app.parser.account_data_parser import (
    classify_account_entry,
    parse_inferences,
    parse_playlist_track_events,
    normalize_inference_genres,
    parse_saved_library,
    parse_your_library,
    parse_sound_capsule,
    parse_wrapped,
)
# Bu testler isim-tabanlı (içeriksiz) tespiti doğrular → from_namelist yardımcısı.
# İçerik-tabanlı detect_zip_type için test_zip_detect.py'a bakın.
from app.services.zip_detect import detect_zip_type_from_namelist as detect_zip_type
from tests.fixtures.account_samples import INFERENCES, YOUR_LIBRARY


# ─── classify_account_entry ──────────────────────────────────────────────────

def test_classify_hard_discard():
    assert classify_account_entry("Identifiers.json") == "hard_discard"
    assert classify_account_entry("UserAttributes.json") == "hard_discard"
    assert classify_account_entry("Identity.json") == "hard_discard"
    assert classify_account_entry("AdsIdentitySecondPartyIdentifiers.json") == "hard_discard"


def test_classify_known_files():
    assert classify_account_entry("YourLibrary.json") == "your_library"
    assert classify_account_entry("Inferences.json") == "inferences"
    assert classify_account_entry("Wrapped2025.json") == "wrapped"
    assert classify_account_entry("YourSoundCapsule.json") == "sound_capsule"
    assert classify_account_entry("Playlist1.json") == "playlist_events"


def test_classify_streaming_in_account_zip():
    assert classify_account_entry("StreamingHistory_music_0.json") == "streaming"


def test_classify_unknown_skip():
    assert classify_account_entry("SomethingElse.json") == "skip"
    assert classify_account_entry("Payments.json") == "skip"


# ─── parse_your_library ───────────────────────────────────────────────────────

def test_parse_library_list_format():
    data = [
        {"uri": "spotify:track:abc123", "name": "Song A"},
        {"uri": "spotify:track:def456", "name": "Song B"},
        {"uri": "spotify:episode:ghi789", "name": "Episode (atlanmalı)"},
    ]
    rows = parse_your_library(data)
    assert len(rows) == 2
    assert all(r["event_type"] == "liked" for r in rows)
    assert rows[0]["spotify_uri"] == "spotify:track:abc123"


def test_parse_library_dict_format():
    data = {
        "tracks": [{"uri": "spotify:track:zzz999"}],
        "albums": [{"uri": "spotify:album:aaa111"}],
    }
    rows = parse_your_library(data)
    assert len(rows) == 1
    assert rows[0]["spotify_uri"] == "spotify:track:zzz999"


def test_parse_library_no_uri_skipped():
    data = [{"name": "Track without URI"}]
    rows = parse_your_library(data)
    assert len(rows) == 0


def test_parse_library_occurred_at_none():
    data = [{"uri": "spotify:track:abc123"}]
    rows = parse_your_library(data)
    assert rows[0]["occurred_at"] is None


# ─── normalize_inference_genres (A12) ────────────────────────────────────────

def test_a12_etiketi_rosso_turune_cevirir():
    labels = [
        "Interest | Music | Hip-hop | Hip-hop(1P)",
        "Interest | Music | EDM | EDM(1P)",
    ]
    assert normalize_inference_genres(labels) == ["electronic", "hip-hop"]


def test_a12_tautolojiyi_atar():
    """"Music | Music" bir tur degil, jenerik kova — motora girmemeli."""
    assert normalize_inference_genres(["Interest | Music | Music | Music(1P)"]) == []


def test_a12_tekrari_teklestirir():
    labels = [
        "Interest | Music | Pop | Pop(1P)",
        "Interest | Music | Pop | Pop(1P)",
    ]
    assert normalize_inference_genres(labels) == ["pop"]


def test_a12_bilinmeyen_etiketi_atar():
    assert normalize_inference_genres(["Interest | Music | Zurna | Zurna(1P)"]) == []


def test_a12_bozuk_girdide_patlamaz():
    assert normalize_inference_genres(["Interest | Music", "", None, 42]) == []
    assert normalize_inference_genres([]) == []
    assert normalize_inference_genres(None) == []


def test_a12_canli_18_etiket_beklenen_kumeyi_verir():
    """Canli veri (Efendim, 2026-08-02): 18 etiket → 17 anlamli tur.

    Tek elenen "Music | Music" tautolojisi. Bu test gercek girdiyle yazildi;
    format degisirse (Spotify sema degistirirse) burada patlar.
    """
    labels = [
        "Interest | Music | Pop | Pop(1P)", "Interest | Music | Trap | Trap(1P)",
        "Interest | Music | Emo | Emo(1P)", "Interest | Music | Soundtrack | Soundtrack(1P)",
        "Interest | Music | Eras Oldies | Eras Oldies(1P)", "Interest | Music | Rock | Rock(1P)",
        "Interest | Music | Indie | Indie(1P)", "Interest | Music | Soul | Soul(1P)",
        "Interest | Music | R&B | R&B(1P)", "Interest | Music | Hip-hop | Hip-hop(1P)",
        "Interest | Music | Metal | Metal(1P)", "Interest | Music | Blues | Blues(1P)",
        "Interest | Music | Punk | Punk(1P)", "Interest | Music | Folk | Folk(1P)",
        "Interest | Music | Jazz | Jazz(1P)", "Interest | Music | Music | Music(1P)",
        "Interest | Music | Classical | Classical(1P)", "Interest | Music | EDM | EDM(1P)",
    ]
    result = normalize_inference_genres(labels)
    assert len(result) == 17
    assert "electronic" in result and "oldies" in result
    assert "music" not in result


# ─── parse_saved_library (A7) ─────────────────────────────────────────────────

def test_saved_library_album_ve_sanatci_okunur():
    """A7: albums + artists artik atilmiyor (once yalniz tracks okunuyordu)."""
    data = {
        "tracks": [{"uri": "spotify:track:t1"}],
        "albums": [{"uri": "spotify:album:a1", "album": "Muptezhel", "artist": "Ezhel"}],
        "artists": [{"uri": "spotify:artist:r1", "name": "Ezhel"}],
    }
    rows = parse_saved_library(data)
    assert len(rows) == 2
    types = {r["item_type"] for r in rows}
    assert types == {"album", "artist"}


def test_saved_library_track_ALMAZ():
    """Track'ler liked_songs_events'te kalir — bu tabloya SIZMAMALI.

    Sizarsa A2/A4 ve taste sorgulari uri'yi track varsayarak bozulurdu.
    """
    data = {"tracks": [{"uri": "spotify:track:t1"}], "albums": [], "artists": []}
    assert parse_saved_library(data) == []


def test_saved_library_yanlis_uri_onekini_eler():
    """albums listesinde track uri'si gelirse alinmaz (savunmaci)."""
    data = {"albums": [{"uri": "spotify:track:sahte"}], "artists": []}
    assert parse_saved_library(data) == []


def test_saved_library_album_adi_album_alanindan_gelir():
    # Albumde "name" yok, "album" var; sanatcida "name" var.
    data = {"albums": [{"uri": "spotify:album:a1", "album": "Muptezhel"}], "artists": []}
    rows = parse_saved_library(data)
    assert rows[0]["name"] == "Muptezhel"


def test_saved_library_liste_formatinda_bos_doner():
    """Eski format (duz liste) albums/artists icermez — patlamamali."""
    assert parse_saved_library([{"uri": "spotify:track:x"}]) == []


def test_saved_library_bos_girdi():
    assert parse_saved_library({}) == []
    assert parse_saved_library(None) == []


# ─── parse_inferences ─────────────────────────────────────────────────────────

def test_inferences_yalniz_music_prefix():
    """Gerçek format: yalnızca 'Interest | Music |' prefix'li etiketler müzik."""
    sig = parse_inferences(INFERENCES)
    # 6 etiketten 3'ü "Interest | Music |" (Pop, Trap, Emo)
    assert sig["music_count"] == 3
    assert any("Pop" in lbl for lbl in sig["music_labels"])
    # Education, Hobbies (Musical Instruments dahil), 1P_Custom, test-* müzik DEĞİL
    assert not any("Education" in lbl for lbl in sig["music_labels"])
    assert not any("Musical Instruments" in lbl for lbl in sig["music_labels"])


def test_parse_inferences_empty():
    result = parse_inferences({"inferences": []})
    assert result["all_count"] == 0
    assert result["music_labels"] == []


def test_parse_inferences_list_format():
    data = ["Interest | Music | Pop | Pop(1P)", "drama_series"]
    result = parse_inferences(data)
    assert result["all_count"] == 2
    assert result["music_count"] == 1


# ─── parse_playlist_track_events ──────────────────────────────────────────────

_PLAYLIST_SAMPLE = {
    "playlists": [
        {
            "name": "Bariyere girmelik",
            "items": [
                {"track": {"trackUri": "spotify:track:16xC6ZAyPx2iJfiAoXCCzn"}, "addedDate": "2026-04-06"},
                {"track": {"trackUri": "spotify:track:5UeIwcUIKTVPqBnuXnhmBD"}, "addedDate": "2026-04-06"},
            ],
        },
        {
            "name": "Boş Liste",
            "items": [],
        },
    ]
}


def test_parse_playlist_events_basic():
    rows = parse_playlist_track_events(_PLAYLIST_SAMPLE)
    assert len(rows) == 2
    assert rows[0]["track_uri"] == "spotify:track:16xC6ZAyPx2iJfiAoXCCzn"
    assert rows[0]["playlist_uri"] == "Bariyere girmelik"
    assert rows[0]["added_at"] == "2026-04-06"


def test_parse_playlist_events_missing_added_date_skipped():
    data = {"playlists": [{"name": "P", "items": [
        {"track": {"trackUri": "spotify:track:abc"}},  # addedDate yok
    ]}]}
    assert parse_playlist_track_events(data) == []


def test_parse_playlist_events_invalid_uri_skipped():
    data = {"playlists": [{"name": "P", "items": [
        {"track": {"trackUri": "spotify:episode:xyz"}, "addedDate": "2026-01-01"},
        {"track": None, "addedDate": "2026-01-01"},
    ]}]}
    assert parse_playlist_track_events(data) == []


def test_parse_playlist_events_no_name_fallback():
    data = {"playlists": [{"items": [
        {"track": {"trackUri": "spotify:track:abc"}, "addedDate": "2026-01-01"},
    ]}]}
    rows = parse_playlist_track_events(data)
    assert rows[0]["playlist_uri"] == "unknown"


def test_parse_playlist_events_non_dict():
    assert parse_playlist_track_events([]) == []
    assert parse_playlist_track_events(None) == []


# ─── detect_zip_type ─────────────────────────────────────────────────────────

def test_detect_streaming_history():
    namelist = [
        "Spotify Extended Streaming History/StreamingHistory_music_0.json",
        "Spotify Extended Streaming History/StreamingHistory_music_1.json",
    ]
    assert detect_zip_type(namelist) == "streaming_history"


def test_detect_account_data():
    namelist = [
        "Spotify Account Data/Inferences.json",
        "Spotify Account Data/YourLibrary.json",
        "Spotify Account Data/Identifiers.json",
    ]
    assert detect_zip_type(namelist) == "account_data"


def test_detect_technical_log():
    namelist = [
        "Spotify Technical Log/AddedToCollection.json",
        "Spotify Technical Log/CarDetectionEvent.json",
    ]
    assert detect_zip_type(namelist) == "technical_log"


def test_detect_unknown():
    namelist = ["SomeRandomFile.json", "AnotherFile.txt"]
    assert detect_zip_type(namelist) == "unknown"


def test_detect_mixed_zip():
    # Spotify Account Data export'u streaming history'yi de içerir → mixed
    namelist = [
        "StreamingHistory_music_0.json",
        "StreamingHistory_music_1.json",
        "YourLibrary.json",
        "Inferences.json",
        "Wrapped2025.json",
    ]
    assert detect_zip_type(namelist) == "mixed"


def test_detect_mixed_with_techlog():
    # Streaming + techlog sinyali → mixed
    namelist = [
        "StreamingHistory_music_0.json",
        "AddedToCollection.json",
    ]
    assert detect_zip_type(namelist) == "mixed"


def test_detect_streaming_only():
    # Yalnızca streaming → streaming_history
    namelist = [
        "StreamingHistory_music_0.json",
        "StreamingHistory_music_1.json",
    ]
    assert detect_zip_type(namelist) == "streaming_history"

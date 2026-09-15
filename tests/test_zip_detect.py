"""İçerik tabanlı ZIP tipi tespiti testleri (gerçek in-memory ZIP'ler)."""
import io
import json
import zipfile

from app.services.zip_detect import detect_zip_type, detect_zip_type_from_namelist
from tests.fixtures.techlog_samples import ADDED_TO_COLLECTION, CAR_DETECTION


def _zip(files: dict[str, object]) -> zipfile.ZipFile:
    """{dosya_adı: python_obj} → in-memory ZIP (her obj JSON dizisine sarılır)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, obj in files.items():
            if isinstance(obj, (list, dict)):
                zf.writestr(name, json.dumps(obj))
            else:
                zf.writestr(name, str(obj))
    buf.seek(0)
    return zipfile.ZipFile(buf)


_STREAM_OBJ = {
    "ts": "2026-01-01T00:00:00Z",
    "ms_played": 180000,
    "master_metadata_track_name": "Song",
    "spotify_track_uri": "spotify:track:abc",
}


def test_streaming_detected_by_name_and_content():
    zf = _zip({"StreamingHistory_music_0.json": [_STREAM_OBJ]})
    assert detect_zip_type(zf) == "streaming_history"


def test_streaming_detected_even_when_renamed():
    # Dosya adı değiştirilmiş ama içerik streaming → yine de tespit edilmeli (asıl hedef)
    zf = _zip({"music_data_0.json": [_STREAM_OBJ]})
    assert detect_zip_type(zf) == "streaming_history"


def test_account_data_detected_by_content():
    zf = _zip({
        "YourLibrary.json": {"tracks": [{"uri": "spotify:track:x", "trackName": "T"}]},
    })
    assert detect_zip_type(zf) == "account_data"


def test_technical_log_detected_by_content():
    # Gerçek Spotify formatı: message_item_uri + message_set + timestamp_utc
    zf = _zip({
        "AddedToCollection.json": ADDED_TO_COLLECTION,
        "CarDetectionEvent.json": CAR_DETECTION,
    })
    assert detect_zip_type(zf) == "technical_log"


def test_mixed_streaming_plus_account():
    zf = _zip({
        "StreamingHistory_music_0.json": [_STREAM_OBJ],
        "YourLibrary.json": {"tracks": [{"uri": "spotify:track:x"}]},
    })
    assert detect_zip_type(zf) == "mixed"


def test_name_only_without_content_is_not_enough():
    # İsim account gibi ama içerik tamamen alakasız (kanıt yok) → unknown.
    # Skor: isim +2, içerik kanıtı yok → 2 < 3 eşik.
    zf = _zip({"YourLibrary.json": [{"completely": "unrelated", "data": 1}]})
    assert detect_zip_type(zf) == "unknown"


def test_unknown_when_no_signals():
    zf = _zip({"random.json": [{"foo": "bar"}], "notes.txt": "hello"})
    assert detect_zip_type(zf) == "unknown"


def test_corrupt_json_does_not_crash():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("StreamingHistory_music_0.json", "{ this is not valid json ]")
        zf.writestr("README.txt", "hi")
    buf.seek(0)
    # Adı StreamingHistory → isim +2 ama içerik bozuk → kanıt yok → 2 < 3 → unknown.
    # Önemli olan: ÇÖKMEZ.
    assert detect_zip_type(zipfile.ZipFile(buf)) == "unknown"


def test_empty_zip_is_unknown():
    zf = _zip({})
    assert detect_zip_type(zf) == "unknown"


def test_technical_log_detected_when_top_level_object():
    # Spotify bazı versiyonlarda DaylistGenerated ve CarDetectionEvent'i
    # top-level obje ({...}) olarak gönderiyor — dizi değil.
    # Bu durumda skor 2'de (isim sinyali) kalıp 'unknown' dönüyordu — regresyon.
    zf = _zip({
        "DaylistGenerated.json": {"message_playlist_title": "morning chill",
                                  "message_daypart": "morning",
                                  "timestamp_utc": "2026-01-01T08:00:00Z"},
    })
    assert detect_zip_type(zf) == "technical_log"


def test_technical_log_detected_car_detection_top_level_object():
    zf = _zip({
        "CarDetectionEvent.json": {"timestamp_utc": "2026-01-01T08:00:00Z",
                                   "message_is_car_connected": True},
    })
    assert detect_zip_type(zf) == "technical_log"


# ── Geriye uyumlu isim-tabanlı yardımcı ──
def test_namelist_helper_streaming():
    assert detect_zip_type_from_namelist(["StreamingHistory_music_0.json"]) == "streaming_history"


def test_namelist_helper_mixed():
    out = detect_zip_type_from_namelist(
        ["StreamingHistory_music_0.json", "AddedToCollection.json"]
    )
    assert out == "mixed"


# ── Zip-bomb guard ────────────────────────────────────────────────────────────
import pytest  # noqa: E402
from app.services.zip_detect import ZipBombError, guard_zip_bomb  # noqa: E402


def _zip_raw(files: dict[str, bytes]) -> zipfile.ZipFile:
    """Ham bayt içerikli in-memory ZIP (sıkıştırma oranını test etmek için)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    buf.seek(0)
    return zipfile.ZipFile(buf)


def test_guard_normal_zip_gecer():
    # Gerçekçi küçük export → guard sorunsuz geçmeli
    zf = _zip({"StreamingHistory_music_0.json": [_STREAM_OBJ] * 10})
    guard_zip_bomb(zf)  # exception yükseltmemeli


def test_guard_yuksek_oran_reddedilir():
    # 50MB sıfır → deflate ile ~KB'ye sıkışır (oran >>100x). Küçük test eşiğiyle
    # (min_compressed=1KB) oran kontrolü devreye girer → reddedilmeli.
    bomb = b"\x00" * (50 * 1024 * 1024)
    zf = _zip_raw({"bomb.json": bomb})
    with pytest.raises(ZipBombError):
        guard_zip_bomb(zf, min_compressed_for_ratio=1024)


def test_guard_toplam_boyut_reddedilir():
    # Açılmış boyut, düşük test eşiğini (1MB) aşarsa reddedilmeli.
    data = b"\x00" * (2 * 1024 * 1024)  # 2MB açılmış
    zf = _zip_raw({"big.json": data})
    with pytest.raises(ZipBombError):
        guard_zip_bomb(zf, max_total=1024 * 1024)


def test_guard_kucuk_dosyada_oran_atlanir():
    # Sıkışmış boyut min eşiğin (üretim 1MB) altındaysa yüksek oran olsa da atlanır.
    small = b"\x00" * (500 * 1024)  # 500KB sıfır → çok sıkışır ama küçük
    zf = _zip_raw({"small.json": small})
    guard_zip_bomb(zf)  # üretim eşikleriyle exception yükseltmemeli

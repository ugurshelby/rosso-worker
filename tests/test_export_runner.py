"""export_runner saf helper testleri — yalın play_events dönüşümü."""
import io
import json
import zipfile

from app.pipeline.export_runner import (
    run_one_export,
    to_play_event_row,
    to_podcast_event_row,
    split_music_podcast,
    _spotify_id_from_uri,
)
from tests.fixtures.techlog_samples import ADDED_TO_COLLECTION


def test_to_play_event_row_omits_raw_fields():
    normalized = {
        "played_at": "2024-01-01T00:00:00Z",
        "content_type": "music",
        "raw_track_name": "Song",
        "raw_artist_name": "Artist",
        "spotify_track_uri": "spotify:track:A",
        "ms_played": 6000,
        "platform": "spotify",
        "source": "spotify_export",
        "conn_country": "TR",
        "reason_start": "trackdone",
        "reason_end": "fwdbtn",
        "shuffle": True,
        "skipped": False,
        "offline": False,
        "incognito_mode": False,
    }
    row = to_play_event_row(normalized, user_id="u1", track_id="track-uuid")
    # Yalın şema: raw alanlar ASLA olmamalı
    assert "raw_track_name" not in row
    assert "raw_artist_name" not in row
    assert "spotify_track_uri" not in row
    assert "content_type" not in row
    # Olması gerekenler
    assert row["track_id"] == "track-uuid"
    assert row["user_id"] == "u1"
    assert row["played_at"] == "2024-01-01T00:00:00Z"
    assert row["ms_played"] == 6000
    assert row["conn_country"] == "TR"
    assert row["reason_end"] == "fwdbtn"
    assert row["shuffle"] is True


def test_to_podcast_event_row():
    normalized = {
        "played_at": "2024-01-01T00:00:00Z",
        "content_type": "podcast",
        "episode_name": "Ep 1",
        "episode_show_name": "Show",
        "spotify_episode_uri": "spotify:episode:E",
        "ms_played": 9000,
        "skipped": False,
        "offline": True,
    }
    row = to_podcast_event_row(normalized, user_id="u1")
    assert row["episode_name"] == "Ep 1"
    assert row["spotify_episode_uri"] == "spotify:episode:E"
    assert row["user_id"] == "u1"
    assert row["offline"] is True


def test_split_music_podcast():
    rows = [
        {"content_type": "music", "spotify_track_uri": "spotify:track:A"},
        {"content_type": "podcast", "spotify_episode_uri": "spotify:episode:E"},
        {"content_type": "music", "spotify_track_uri": "spotify:track:B"},
        {"content_type": "unknown"},
    ]
    music, podcast = split_music_podcast(rows)
    assert len(music) == 2
    assert len(podcast) == 1  # unknown atılır


def test_spotify_id_from_uri():
    assert _spotify_id_from_uri("spotify:track:2oW85y") == "2oW85y"
    assert _spotify_id_from_uri("spotify:episode:E") is None
    assert _spotify_id_from_uri(None) is None


# ─── run_one_export: mixed ZIP ───────────────────────────────────────────────

_STREAM_EVENT = {
    "ts": "2026-01-01T00:00:00Z",
    "ms_played": 180000,
    "master_metadata_track_name": "Song",
    "master_metadata_album_artist_name": "Artist",
    "spotify_track_uri": "spotify:track:2oW85yArtB8wcWNVPC3Cot",
    "reason_start": "trackdone",
    "reason_end": "trackdone",
}


class _FakeTable:
    """Boş bir DB gibi davranır: upsert edilen her satır YENİ eklenmiş sayılır
    (gerçek PostgREST ignore_duplicates + return=representation'da yalnız
    eklenenleri döndürür). `already_present=True` → her şey zaten DB'de
    (ikinci ZIP senaryosu), hiçbir satır dönmez."""

    def __init__(self, sink, already_present=False):
        self._sink = sink
        self._already_present = already_present
        self._last: list = []

    def upsert(self, rows, **kwargs):
        self._sink.append(rows)
        self._last = [] if self._already_present else list(rows)
        return self

    def execute(self):
        return type("R", (), {"data": self._last})()


class _FakeStorage:
    def __init__(self, blob):
        self._blob = blob
        self.removed = []

    def from_(self, bucket):
        return self

    def download(self, path):
        return self._blob

    def remove(self, paths):
        self.removed.extend(paths)


class _FakeClient:
    def __init__(self, blob, already_present=False):
        self.storage = _FakeStorage(blob)
        self.upserts = []
        self._already_present = already_present

    def table(self, name):
        return _FakeTable(self.upserts, self._already_present)

    def rpc(self, fn, params):
        # insert_tracks_batch → her track için {spotify_id, track_id}
        tracks = params.get("p_tracks", [])
        data = [{"spotify_id": t["spotify_id"], "track_id": "tid-" + t["spotify_id"]}
                for t in tracks]
        return type("R", (), {"execute": lambda self_=None: type("X", (), {"data": data})()})()


class _FakeSettings:
    export_bucket = "exports"
    batch_size = 500
    spotify_client_id = ""       # boş → artist lookup atlanır
    spotify_client_secret = ""


def _mixed_zip_blob() -> bytes:
    """Streaming + account (YourLibrary) aynı ZIP'te → mixed."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("StreamingHistory_music_0.json", json.dumps([_STREAM_EVENT]))
        zf.writestr("YourLibrary.json", json.dumps({
            "tracks": [{"uri": "spotify:track:1UjF2lJacofdLCS6ipE95q",
                        "artist": "A", "album": "B", "track": "C"}],
        }))
        zf.writestr("AddedToCollection.json", json.dumps(ADDED_TO_COLLECTION))
    return buf.getvalue()


def test_mixed_zip_hem_streaming_hem_yan_veri_isler():
    client = _FakeClient(_mixed_zip_blob())
    job = {"id": "job-1", "user_id": "u1", "file_path": "u1/export.zip"}

    result = run_one_export(client, _FakeSettings(), job)

    assert result["outcome"] == "success"
    assert result["events"] > 0            # streaming akışı işlendi
    # yan-veri (account + techlog) sayıları counts'ta
    assert result["counts"]["liked_songs_inserted"] > 0
    # Karne sözleşmesi (2026-07-19): tip + matched/skipped raporlanır
    assert result["zip_type"] == "mixed"
    # matched = play + podcast + yan-veri yazımları — en az streaming + liked
    assert result["matched_events"] >= result["inserted_events"] + result["counts"]["liked_songs_inserted"]
    assert result["skipped_events"] >= 0


def test_saf_yan_veri_zip_karnesi():
    """account_data-only ZIP: events=0 ama matched yan-veri yazımlarını sayar."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("YourLibrary.json", json.dumps({
            "tracks": [
                {"uri": "spotify:track:1UjF2lJacofdLCS6ipE95q"},
                {"uri": "spotify:track:2oW85yArtB8wcWNVPC3Cot"},
            ],
        }))
    client = _FakeClient(buf.getvalue())
    job = {"id": "job-2", "user_id": "u1", "file_path": "u1/account.zip"}

    result = run_one_export(client, _FakeSettings(), job)

    assert result["outcome"] == "success"
    assert result["zip_type"] == "account_data"
    assert result["events"] == 0
    assert result["matched_events"] == 2   # 2 liked-snapshot kaydı yazıldı
    assert result["skipped_events"] == 0
    assert result["tracks"] == 0


class _CredSettings:
    """Spotify credential DOLU — eski kodda lookup tetiklenirdi."""
    export_bucket = "exports"
    batch_size = 500
    spotify_client_id = "cid"
    spotify_client_secret = "secret"


def _streaming_zip_blob() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("StreamingHistory_music_0.json", json.dumps([_STREAM_EVENT]))
    return buf.getvalue()


def test_export_spotify_lookup_yapmaz(monkeypatch):
    """Lookup enrichment'a taşındı: credential dolu olsa bile export Spotify'a
    DOKUNMAZ. get_track_artist_ids çağrılırsa test patlar; dönüşte quota anahtarı yok."""
    import app.services.spotify_artist as sa

    def _boom(*a, **k):
        raise AssertionError("export artık Spotify lookup yapmamalı")

    monkeypatch.setattr(sa, "get_track_artist_ids", _boom)

    client = _FakeClient(_streaming_zip_blob())
    job = {"id": "job-x", "user_id": "u1", "file_path": "u1/e.zip"}
    result = run_one_export(client, _CredSettings(), job)

    assert result["outcome"] == "success"
    assert result["events"] > 0
    assert "quota_hit_artist_ids" not in result


# ── api_realtime örtüşmesi (ÖLÇÜLDÜ 2026-09-21) ──────────────────────────────
# ZIP `ts`'i tam saniye, canlı dinleme milisaniyeli: aynı çalma, tam eşleşme
# arayan dedup index'inden kaçıyordu. Canlı satırlar ZIP'ten ÖNCE geldiğinde
# (ikinci ZIP senaryosu) dinlemeler ikiye katlanacaktı.

import pytest

from app.pipeline.export_runner import NEARBY_WINDOW_SECONDS, _epoch, drop_near_realtime


def _row(track_id: str, played_at: str) -> dict:
    return {"track_id": track_id, "played_at": played_at}


def test_epoch_z_ve_milisaniye():
    assert _epoch("2026-07-02T10:18:03Z") == _epoch("2026-07-02T10:18:03+00:00")
    assert _epoch("2026-07-02T10:18:03.816+00:00") - _epoch("2026-07-02T10:18:03Z") == pytest.approx(0.816)
    assert _epoch(None) is None
    assert _epoch("bozuk") is None


def test_ayni_calma_milisaniye_farkiyla_atilir():
    rt = {"t1": [_epoch("2026-07-02T10:18:03.816+00:00")]}
    kalan, atilan = drop_near_realtime([_row("t1", "2026-07-02T10:18:03Z")], rt)
    assert kalan == [] and atilan == 1


def test_pencere_sinirinda_atilir_disinda_kalir():
    base = _epoch("2026-07-02T10:18:00Z")
    rt = {"t1": [base]}
    icinde = _row("t1", "2026-07-02T10:18:05Z")   # tam +5 sn
    disinda = _row("t1", "2026-07-02T10:18:06Z")  # +6 sn
    kalan, atilan = drop_near_realtime([icinde, disinda], rt)
    assert NEARBY_WINDOW_SECONDS == 5
    assert atilan == 1 and kalan == [disinda]


def test_farkli_sarki_ayni_an_atilmaz():
    rt = {"t1": [_epoch("2026-07-02T10:18:03Z")]}
    satir = _row("t2", "2026-07-02T10:18:03Z")
    kalan, atilan = drop_near_realtime([satir], rt)
    assert kalan == [satir] and atilan == 0


def test_canli_veri_yoksa_hepsi_kalir():
    satirlar = [_row("t1", "2026-07-02T10:18:03Z"), _row("t2", "2026-07-03T10:00:00Z")]
    kalan, atilan = drop_near_realtime(satirlar, {})
    assert kalan == satirlar and atilan == 0


def test_ayni_sarki_gun_icinde_iki_kez_yalniz_eslesen_atilir():
    rt = {"t1": [_epoch("2026-07-02T10:18:03.5+00:00")]}
    sabah = _row("t1", "2026-07-02T10:18:03Z")
    aksam = _row("t1", "2026-07-02T21:40:00Z")
    kalan, atilan = drop_near_realtime([sabah, aksam], rt)
    assert kalan == [aksam] and atilan == 1


def test_ikinci_zip_zaten_olan_calmalari_yazildi_saymaz():
    """ÖLÇÜLDÜ 2026-09-21: karne yazılmaya DENENEN satırı sayıyordu. İkinci ZIP'te
    önceki ZIP'in çalmaları zaten DB'de; hepsi ATLANDI olarak raporlanmalı."""
    client = _FakeClient(_streaming_zip_blob(), already_present=True)
    job = {"id": "job-2", "user_id": "u1", "file_path": "u1/e2.zip"}
    result = run_one_export(client, _FakeSettings(), job)

    assert result["outcome"] == "success"
    assert result["events"] > 0              # yazmaya denendi
    assert result["inserted_events"] == 0    # ama hiçbiri yeni değildi
    assert result["matched_events"] == 0
    assert result["skipped_events"] >= result["events"]

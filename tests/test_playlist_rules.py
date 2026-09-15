"""Tests for auto-playlist rule engine (plan §7.1)."""
import calendar
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from app.playlist_rules.rules import (
    RuleTrack,
    top_month,
    top_year,
    morning_routine,
    nostalgia,
    most_skipped,
    obsession,
)


def _make_row(track_id: str, play_count: int = 5) -> dict:
    return {
        "track_id": track_id,
        "raw_track_name": f"Song {track_id}",
        "raw_artist_name": "Artist",
        "play_count": play_count,
    }


def _mock_rpc(rows: list[dict]):
    """Returns a mock that mimics db.rpc(...).execute()."""
    rpc_mock = MagicMock()
    rpc_mock.execute.return_value = MagicMock(data=rows)
    db_mock = MagicMock()
    db_mock.rpc.return_value = rpc_mock
    return db_mock


@patch("app.playlist_rules.rules._db")
class TestTopMonth:
    def test_returns_tracks_for_month(self, mock_db):
        rows = [_make_row("t1", 10), _make_row("t2", 5)]
        mock_db.return_value = _mock_rpc(rows)

        result = top_month("user-1", 2025, 4, n=50)

        assert len(result) == 2
        assert result[0].track_id == "t1"
        assert result[0].play_count == 10

    def test_passes_correct_date_range(self, mock_db):
        db = _mock_rpc([])
        mock_db.return_value = db

        top_month("user-1", 2025, 2, n=50)

        call_kwargs = db.rpc.call_args[1]
        params = call_kwargs["params"] if "params" in call_kwargs else db.rpc.call_args[0][1]
        assert "2025-02-01" in params["p_from"]
        assert "2025-02-28" in params["p_to"]  # Feb 2025 has 28 days

    def test_february_leap_year(self, mock_db):
        db = _mock_rpc([])
        mock_db.return_value = db

        top_month("user-1", 2024, 2, n=50)

        call_kwargs = db.rpc.call_args[1]
        params = call_kwargs["params"] if "params" in call_kwargs else db.rpc.call_args[0][1]
        assert "2024-02-29" in params["p_to"]  # 2024 is a leap year

    def test_skips_rows_without_track_id(self, mock_db):
        rows = [
            {"track_id": None, "raw_track_name": "x", "play_count": 5},
            _make_row("t2", 3),
        ]
        mock_db.return_value = _mock_rpc(rows)

        result = top_month("user-1", 2025, 1, n=50)

        assert len(result) == 1
        assert result[0].track_id == "t2"


@patch("app.playlist_rules.rules._db")
class TestTopYear:
    def test_returns_full_year_range(self, mock_db):
        db = _mock_rpc([])
        mock_db.return_value = db

        top_year("user-1", 2024, n=50)

        params = db.rpc.call_args[0][1]
        assert params["p_from"].startswith("2024-01-01")
        assert params["p_to"].startswith("2024-12-31")

    def test_returns_tracks(self, mock_db):
        rows = [_make_row(f"t{i}", 10 - i) for i in range(5)]
        mock_db.return_value = _mock_rpc(rows)

        result = top_year("user-1", 2024, n=5)
        assert len(result) == 5


@patch("app.playlist_rules.rules._db")
class TestMorningRoutine:
    def test_passes_hour_filter(self, mock_db):
        db = _mock_rpc([])
        mock_db.return_value = db

        morning_routine("user-1", n=20)

        params = db.rpc.call_args[0][1]
        assert params["p_hour_from"] == 6
        assert params["p_hour_to"] == 9

    def test_n_parameter_propagated(self, mock_db):
        db = _mock_rpc([])
        mock_db.return_value = db

        morning_routine("user-1", n=20)

        params = db.rpc.call_args[0][1]
        assert params["p_limit"] == 20


@patch("app.playlist_rules.rules._db")
class TestMostSkipped:
    def test_passes_skipped_flag(self, mock_db):
        db = _mock_rpc([])
        mock_db.return_value = db

        most_skipped("user-1", n=50)

        params = db.rpc.call_args[0][1]
        assert params["p_skipped"] is True

    def test_returns_skipped_tracks(self, mock_db):
        rows = [_make_row("skip1", 30), _make_row("skip2", 20)]
        mock_db.return_value = _mock_rpc(rows)

        result = most_skipped("user-1", n=50)
        assert len(result) == 2


@patch("app.playlist_rules.rules._db")
class TestObsession:
    def test_passes_min_plays(self, mock_db):
        db = _mock_rpc([])
        mock_db.return_value = db

        obsession("user-1", n=50)

        params = db.rpc.call_args[0][1]
        assert params["p_min_plays"] == 10

    def test_date_range_is_30_days(self, mock_db):
        db = _mock_rpc([])
        mock_db.return_value = db

        before = datetime.now(timezone.utc)
        obsession("user-1", n=50)
        after = datetime.now(timezone.utc)

        params = db.rpc.call_args[0][1]
        from_dt = datetime.fromisoformat(params["p_from"])
        # from_dt should be ~30 days before now
        diff = after - from_dt
        assert 29 <= diff.days <= 31


@patch("app.playlist_rules.rules._db")
class TestNostalgia:
    def test_returns_tracks_from_year(self, mock_db):
        rows = [_make_row("old1", 8), _make_row("old2", 4)]
        mock_db.return_value = _mock_rpc(rows)

        result = nostalgia("user-1", year=2019, n=50)

        assert len(result) == 2
        params = mock_db.return_value.rpc.call_args[0][1]
        assert params["p_from"].startswith("2019-01-01")
        assert params["p_to"].startswith("2019-12-31")


@patch("app.playlist_rules.rules._db")
class TestSortBy:
    """P4.1-worker: kuralın sıralama ölçütü RPC'ye ULAŞIYOR mu?

    Bu sınıfın varlık sebebi ölçülmüş bir kırıktır: `sort_by` DB'de ve
    arayüzde hazırdı, kullanıcı seçebiliyordu, seçim kaydediliyordu — ama
    worker `p_sort_by` göndermediği için üretim hep çalma sayısına göre
    yapılıyordu. Sessiz kırık: hata yok, log yok, liste "makul" görünüyor.
    """

    def test_varsayilan_plays_gonderir(self, mock_db):
        """Ölçüt verilmezse eski davranış korunur (geriye dönük uyum)."""
        db = _mock_rpc([])
        mock_db.return_value = db

        top_month("user-1", 2025, 4, n=50)

        assert db.rpc.call_args[0][1]["p_sort_by"] == "plays"

    def test_duration_rpcye_iletilir(self, mock_db):
        db = _mock_rpc([])
        mock_db.return_value = db

        top_month("user-1", 2025, 4, n=50, sort_by="duration")

        assert db.rpc.call_args[0][1]["p_sort_by"] == "duration"

    def test_top_year_de_iletir(self, mock_db):
        db = _mock_rpc([])
        mock_db.return_value = db

        top_year("user-1", 2024, n=50, sort_by="duration")

        assert db.rpc.call_args[0][1]["p_sort_by"] == "duration"

    def test_bilinmeyen_olcut_playse_duser(self, mock_db):
        """Tanınmayan değer RPC'ye OLDUĞU GİBİ gitmemeli."""
        db = _mock_rpc([])
        mock_db.return_value = db

        top_month("user-1", 2025, 4, n=50, sort_by="rastgele")

        assert db.rpc.call_args[0][1]["p_sort_by"] == "plays"

    def test_duration_secilince_score_sureden_gelir(self, mock_db):
        """score sıralamanın dayandığı büyüklüğü göstermeli."""
        rows = [{**_make_row("t1", 3), "total_ms": 900_000}]
        mock_db.return_value = _mock_rpc(rows)

        result = top_month("user-1", 2025, 4, n=50, sort_by="duration")

        assert result[0].score == 900_000
        assert result[0].play_count == 3

    def test_plays_secilince_score_calma_sayisidir(self, mock_db):
        rows = [{**_make_row("t1", 3), "total_ms": 900_000}]
        mock_db.return_value = _mock_rpc(rows)

        result = top_month("user-1", 2025, 4, n=50)

        assert result[0].score == 3

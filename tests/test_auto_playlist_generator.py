"""Tests for auto_playlist_generator (plan §7.2)."""
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.jobs.auto_playlist_generator import (
    _previous_month,
    _month_playlist_name,
    _year_playlist_name,
    _cover_path_for,
    MONTH_NAMES,
)


class TestCoverPath:
    """Varsayılan kapak seçimi (2026-07-25) — dosya yoksa None (güvenli atla)."""

    def test_aylik_kapak_ay_numarasiyla_secilir(self):
        # Dosya diskte yoksa None döner (henüz eklenmedi) — ama mantık ay-bazlı.
        p = _cover_path_for("top_month", 5)
        # None ya da .../monthly/05.jpg ile biten yol.
        assert p is None or str(p).endswith("monthly/05.jpg") or str(p).endswith("monthly\\05.jpg")

    def test_yillik_kapak_annual_dosyasi(self):
        p = _cover_path_for("top_year", 1)
        assert p is None or str(p).endswith("annual.jpg")


class TestPreviousMonth:
    def test_returns_previous_month(self):
        """_previous_month should always return month before current."""
        year, month = _previous_month()
        now = datetime.now(timezone.utc)
        # previous month
        expected_dt = (now.replace(day=1) - timedelta(days=1))
        assert year == expected_dt.year
        assert month == expected_dt.month

    def test_january_wraps_to_december_previous_year(self):
        """If called in January, should return December of previous year."""
        with patch("app.jobs.auto_playlist_generator.datetime") as mock_dt:
            mock_now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = mock_now

            year, month = _previous_month()

        assert year == 2025
        assert month == 12

    def test_june_returns_may(self):
        """Cron on June 1 → playlist for May."""
        with patch("app.jobs.auto_playlist_generator.datetime") as mock_dt:
            mock_now = datetime(2026, 6, 1, 1, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = mock_now

            year, month = _previous_month()

        assert year == 2026
        assert month == 5


class TestPlaylistName:
    def test_month_name_format(self):
        """Kullanıcı kararı 2026-07-09: 'Mayıs - 2026' (Top-N YOK)."""
        assert _month_playlist_name(2026, 5) == "May - 2026"

    def test_month_name_aralik(self):
        assert _month_playlist_name(2025, 12) == "December - 2025"

    def test_year_name_only_year(self):
        """Yıllık playlist adı yalnız yıl: '2025'."""
        assert _year_playlist_name(2025) == "2025"

    def test_previous_month_name_not_current(self):
        """Cron on June 1 → 'May - 2026', not 'June'."""
        with patch("app.jobs.auto_playlist_generator.datetime") as mock_dt:
            mock_now = datetime(2026, 6, 1, 1, 0, tzinfo=timezone.utc)
            mock_dt.now.return_value = mock_now
            year, month = _previous_month()

        name = _month_playlist_name(year, month)
        assert name == "May - 2026"
        assert "June" not in name


class TestResolveTrackIds:
    @patch("app.jobs.auto_playlist_generator._db")
    def test_returns_spotify_ids(self, mock_db):
        from app.jobs.auto_playlist_generator import _resolve_track_ids, RuleTrack

        rows = [
            {"id": "uuid-1", "spotify_id": "sp1"},
            {"id": "uuid-2", "spotify_id": "sp2"},
        ]
        table_mock = MagicMock()
        table_mock.select.return_value.in_.return_value.execute.return_value = MagicMock(data=rows)
        mock_db.return_value.table.return_value = table_mock

        tracks = [
            RuleTrack("uuid-1", "Song A", "Artist", 10, 10.0),
            RuleTrack("uuid-2", "Song B", "Artist", 5, 5.0),
        ]
        result = _resolve_track_ids(tracks, "spotify")
        assert result == ["sp1", "sp2"]

    @patch("app.jobs.auto_playlist_generator._db")
    def test_returns_empty_for_unknown_platform(self, mock_db):
        from app.jobs.auto_playlist_generator import _resolve_track_ids, RuleTrack

        tracks = [RuleTrack("uuid-1", "Song A", "Artist", 10, 10.0)]
        result = _resolve_track_ids(tracks, "tidal")
        assert result == []

    @patch("app.jobs.auto_playlist_generator._db")
    def test_skips_tracks_without_platform_id(self, mock_db):
        from app.jobs.auto_playlist_generator import _resolve_track_ids, RuleTrack

        rows = [
            {"id": "uuid-1", "spotify_id": None},
            {"id": "uuid-2", "spotify_id": "sp2"},
        ]
        table_mock = MagicMock()
        table_mock.select.return_value.in_.return_value.execute.return_value = MagicMock(data=rows)
        mock_db.return_value.table.return_value = table_mock

        tracks = [
            RuleTrack("uuid-1", "Song A", "Artist", 10, 10.0),
            RuleTrack("uuid-2", "Song B", "Artist", 5, 5.0),
        ]
        result = _resolve_track_ids(tracks, "spotify")
        assert result == ["sp2"]


class TestCreateSpotifyPlaylist:
    """B13 (2026-07-10): create POST'u kaldırılmış /users/{id}/playlists ucuna
    DEĞİL, yeni /me/playlists ucuna gitmeli.

    ⚠ Plan 08 (2026-08-05): `_create_spotify_playlist` artık merkezî geçitten
    (`api_gate.check`) geçiyor. Testlerde geçit AÇIK varsayılır ve `_db()`
    gerçek Supabase istemcisi kurmaya çalışmasın diye mock'lanır — geçidin
    kendi davranışı `test_api_gate.py`'de ayrıca sınanır.
    """

    @patch("app.jobs.auto_playlist_generator._db", lambda: None)
    @patch("app.services.api_gate.refund", lambda c, s, n=1: None)
    @patch(
        "app.services.api_gate.check",
        lambda c, s, **k: __import__(
            "app.services.api_gate", fromlist=["GateDecision"]
        ).GateDecision(allowed=True, reason="ok", remaining=299, used=1, budget=300),
    )
    @patch("app.jobs.auto_playlist_generator.httpx.AsyncClient")
    @patch("app.jobs.auto_playlist_generator._decrypt_token", lambda c: "plain-token")
    @patch("app.jobs.auto_playlist_generator._get_platform_token")
    def test_create_uses_me_playlists_not_removed_endpoint(self, mock_tokens, mock_client_cls):
        import asyncio
        from app.jobs.auto_playlist_generator import _create_spotify_playlist

        mock_tokens.return_value = {"is_active": True, "access_token": "enc"}

        posted_urls = []

        class _Resp:
            def __init__(self, status, payload):
                self.status_code = status
                self._payload = payload
            def json(self):
                return self._payload

        put_urls = []

        class _FakeClient:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def get(self, url, **k):
                return _Resp(200, {"id": "me-user-id"})
            async def post(self, url, **k):
                posted_urls.append(url)
                return _Resp(201, {"id": "new-playlist"})
            async def put(self, url, **k):
                put_urls.append(url)
                return _Resp(200, {})

        mock_client_cls.return_value = _FakeClient()

        result = asyncio.run(_create_spotify_playlist("u1", "Mayıs - 2026", ["t1", "t2"]))

        assert result == "new-playlist"
        create_url = posted_urls[0]
        assert create_url == "https://api.spotify.com/v1/me/playlists"
        # kaldırılmış uç ASLA kullanılmamalı
        assert not any("/users/" in u for u in posted_urls)
        # 2026-08-05: follow çağrısı KALDIRILDI. `POST /me/playlists` listeyi
        # zaten kütüphaneye ekliyor (canlıda ölçüldü: contains -> [true]).
        # Eski `/followers` ucu Şubat 2026'da kaldırıldı (403); halefi
        # `PUT /me/library` ise zaten üye olana 500 veriyor.
        assert not any("/followers" in u for u in put_urls), (
            "kaldırılmış /followers ucu çağrılmamalı"
        )
        assert not any("/me/library" in u for u in put_urls), (
            "gereksiz follow: oluşturma zaten kütüphaneye ekliyor"
        )

    @patch("app.jobs.auto_playlist_generator._db", lambda: None)
    @patch("app.services.api_gate.refund", lambda c, s, n=1: None)
    @patch(
        "app.services.api_gate.check",
        lambda c, s, **k: __import__(
            "app.services.api_gate", fromlist=["GateDecision"]
        ).GateDecision(allowed=True, reason="ok", remaining=299, used=1, budget=300),
    )
    @patch("app.jobs.auto_playlist_generator.httpx.AsyncClient")
    @patch("app.jobs.auto_playlist_generator._decrypt_token", lambda c: "plain-token")
    @patch("app.jobs.auto_playlist_generator._get_platform_token")
    def test_follow_ve_kapak_hatasi_playlisti_DUSURMEZ(self, mock_tokens, mock_client_cls):
        """Follow/kapak İKİNCİL — 403/hata olsa da playlist ID yine döner."""
        import asyncio
        from pathlib import Path
        from app.jobs.auto_playlist_generator import _create_spotify_playlist

        mock_tokens.return_value = {"is_active": True, "access_token": "enc"}

        class _Resp:
            def __init__(self, status, payload=None):
                self.status_code = status
                self._payload = payload or {}
            def json(self):
                return self._payload

        class _FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, url, **k): return _Resp(200, {"id": "me"})
            async def post(self, url, **k): return _Resp(201, {"id": "pl-x"})
            async def put(self, url, **k): return _Resp(403)  # follow + kapak PATLAR

        mock_client_cls.return_value = _FakeClient()

        # Var olmayan kapak yolu → kod is_file() ile atlamalı; follow 403 → yut.
        result = asyncio.run(
            _create_spotify_playlist("u1", "2025", ["t1"], cover_path=Path("/yok/kapak.jpg"))
        )
        assert result == "pl-x", "follow/kapak hatası playlist'i düşürdü"


class TestDecryptToken:
    """B14 (2026-09-01): auto_playlist `decrypt` yerine `decrypt_token` çağırmalı."""

    def test_decrypt_token_uses_cipher_module(self):
        from app.jobs.auto_playlist_generator import _decrypt_token

        with patch("app.services.token_cipher.decrypt_token", return_value="plain") as mock:
            assert _decrypt_token("cipher:blob") == "plain"
            mock.assert_called_once_with("cipher:blob")


class TestPeriodDedup:
    """Başarılı üretim bu dönemde varsa atla; başarısız deneme engellemez."""

    def test_aylik_bu_ay_zaten_uretildiyse_atlanir(self):
        pushed, _ = TestYearlyRuleTiming()._run_in_month(
            9, [TestYearlyRuleTiming()._rule("top_month")], already_produced=True,
        )
        assert pushed == []

    def test_yillik_basarisiz_deneme_yeniden_dener(self):
        """already_produced=False → başarısız geçmiş olsa bile üretilir."""
        pushed, _ = TestYearlyRuleTiming()._run_in_month(
            8, [TestYearlyRuleTiming()._rule("top_year")], already_produced=False,
        )
        assert len(pushed) == 1


class TestYearlyRuleTiming:
    """FAZ AUTO-PL-V2: yıllık kuralın üretim ritmi.

    2026-07-21: yıllık motor yazılmıştı ama davranışı kilitleyen test yoktu.
    2026-08-12 (Efendim): "yalnız Ocak" kısıtlaması kaldırıldı — yıl ortasında
    kuralı açan kullanıcı artık Ocak'a dek beklemiyor, bu yıl içinde HENÜZ
    üretilmediyse (last_run_at'in yılı ≠ bu yıl) ilk çalışmada üretilir.
    "Yılda bir kez" garantisi korunuyor, yalnızca "hangi ay" şartı gevşedi.
    """

    def _rule(self, rule_type, last_run_at=None):
        return {
            "id": "r1",
            "user_id": "u1",
            "rule_type": rule_type,
            "track_count": 50,
            "target_platforms": ["spotify"],
            "name_format": None,
            "enabled": True,
            "last_run_at": last_run_at,
        }

    def _run_in_month(self, month, rules, *, already_produced=False):
        """Belirli bir ayda job'ı koştur; push'u yakalayıp hangi kuralların
        işlendiğini döndür."""
        import asyncio
        from app.jobs import auto_playlist_generator as gen

        pushed: list[str] = []

        db = MagicMock()
        # rules sorgusu
        db.table.return_value.select.return_value.eq.return_value.in_.return_value.execute.return_value.data = rules
        # last_run_at update zinciri
        db.table.return_value.update.return_value.eq.return_value.execute.return_value = None

        async def fake_push(db_, *, rule_id, user_id, playlist_name, tracks, target_platforms, cover_path=None):
            pushed.append(f"{rule_id}:{playlist_name}")
            return 1

        with patch.object(gen, "_db", return_value=db), \
             patch.object(gen, "datetime") as mock_dt, \
             patch.object(gen, "top_month", return_value=[MagicMock(track_id="t1")]), \
             patch.object(gen, "top_year", return_value=[MagicMock(track_id="t1")]), \
             patch.object(gen, "_already_produced_this_period", return_value=already_produced), \
             patch.object(gen, "_push_rule_to_platforms", side_effect=fake_push):
            mock_dt.now.return_value = datetime(2026, month, 1, 1, 0, tzinfo=timezone.utc)
            result = asyncio.run(gen.run_auto_playlist_job())

        return pushed, result

    def test_yillik_bu_yil_hic_calismadiysa_temmuzda_da_uretilir(self):
        """Temmuz'da last_run_at YOK (yeni kural) → hemen üretilir."""
        pushed, _ = self._run_in_month(7, [self._rule("top_year")])
        assert len(pushed) == 1, "last_run_at yoksa yıllık kural her ay üretilebilmeli"
        assert pushed[0].endswith(":2025"), f"beklenen '2025', gelen {pushed[0]}"

    def test_yillik_bu_yil_zaten_calistiysa_tekrar_uretilmez(self):
        """Bu yıl başarılı üretim varsa tekrar üretilmez (yılda 1 kez)."""
        rule = self._rule("top_year", last_run_at="2026-01-01T01:00:00+00:00")
        pushed, _ = self._run_in_month(3, [rule], already_produced=True)
        assert pushed == [], "bu yıl zaten üretildiyse yıllık kural tekrar üretilmemeli"

    def test_yillik_gecen_yil_calismissa_bu_yil_tekrar_uretilir(self):
        """last_run_at geçen yıldan → bu yıl yeniden üretilir (yeni yıl döngüsü)."""
        rule = self._rule("top_year", last_run_at="2025-03-01T01:00:00+00:00")
        pushed, _ = self._run_in_month(7, [rule])
        assert len(pushed) == 1, "geçen yıldan kalan last_run_at bu yılı engellemez"

    def test_yillik_ocakta_uretilir_onceki_yil_adiyla(self):
        """Ocak 2026'da yıllık kural '2025' adıyla üretilir."""
        pushed, _ = self._run_in_month(1, [self._rule("top_year")])
        assert len(pushed) == 1
        assert pushed[0].endswith(":2025"), f"beklenen '2025', gelen {pushed[0]}"

    def test_aylik_her_ay_uretilir(self):
        """Aylık kural Ocak-dışı ayda da çalışır (yıllıkla karışmaz)."""
        pushed, _ = self._run_in_month(7, [self._rule("top_month")])
        assert len(pushed) == 1
        # Temmuz'da çalışınca bir önceki ay = June 2026 (İngilizce ürün dili)
        assert pushed[0].endswith(":June - 2026")

    def test_ocakta_hem_aylik_hem_yillik_calisir(self):
        """Ocak'ta iki kural birden: aylık (December) + yıllık (2025)."""
        pushed, _ = self._run_in_month(
            1, [self._rule("top_month"), self._rule("top_year")]
        )
        names = {p.split(":", 1)[1] for p in pushed}
        assert "December - 2025" in names  # bir önceki ay (İngilizce ürün dili)
        assert "2025" in names           # bir önceki yıl


class TestSortByIletimi:
    """P4.1-worker: kuralın `sort_by` alanı kural motoruna ULAŞIYOR mu?

    Ölçülmüş kırık: kolon (0278) ve arayüz hazırdı, kullanıcı "en çok süre"
    seçebiliyor ve seçim kaydediliyordu — ama üretici alanı SELECT'e bile
    almadığı için her liste çalma sayısına göre üretiliyordu. Sessiz kırık:
    hata yok, log yok, üretilen liste "makul" görünüyor. Ancak kullanıcının
    seçimiyle sonucu karşılaştıran bir kontrol yakalar.
    """

    def _rule(self, rule_type, sort_by="plays", last_run_at=None):
        return {
            "id": "r1",
            "user_id": "u1",
            "rule_type": rule_type,
            "track_count": 50,
            "target_platforms": ["spotify"],
            "name_format": None,
            "enabled": True,
            "last_run_at": last_run_at,
            "sort_by": sort_by,
        }

    def _kural_motoru_cagrisi(self, rules, fonksiyon):
        """Job'ı koştur, kural motorunun aldığı kwargs'ı döndür."""
        import asyncio
        from app.jobs import auto_playlist_generator as gen

        db = MagicMock()
        db.table.return_value.select.return_value.eq.return_value.in_.return_value.execute.return_value.data = rules
        db.table.return_value.update.return_value.eq.return_value.execute.return_value = None

        async def fake_push(db_, **kwargs):
            return 1

        motor = MagicMock(return_value=[MagicMock(track_id="t1")])
        digeri = MagicMock(return_value=[MagicMock(track_id="t1")])
        aylik, yillik = (
            (motor, digeri) if fonksiyon == "top_month" else (digeri, motor)
        )

        with patch.object(gen, "_db", return_value=db), \
             patch.object(gen, "datetime") as mock_dt, \
             patch.object(gen, "top_month", aylik), \
             patch.object(gen, "top_year", yillik), \
             patch.object(gen, "_already_produced_this_period", return_value=False), \
             patch.object(gen, "_push_rule_to_platforms", side_effect=fake_push):
            mock_dt.now.return_value = datetime(2026, 7, 1, 1, 0, tzinfo=timezone.utc)
            asyncio.run(gen.run_auto_playlist_job())

        assert motor.called, "kural motoru hiç çağrılmadı"
        return motor.call_args[1]

    def test_aylik_kuralda_duration_iletilir(self):
        kwargs = self._kural_motoru_cagrisi(
            [self._rule("top_month", sort_by="duration")], "top_month"
        )
        assert kwargs["sort_by"] == "duration"

    def test_yillik_kuralda_duration_iletilir(self):
        kwargs = self._kural_motoru_cagrisi(
            [self._rule("top_year", sort_by="duration")], "top_year"
        )
        assert kwargs["sort_by"] == "duration"

    def test_plays_secili_kuralda_plays_iletilir(self):
        kwargs = self._kural_motoru_cagrisi(
            [self._rule("top_month", sort_by="plays")], "top_month"
        )
        assert kwargs["sort_by"] == "plays"

    def test_alan_null_ise_playse_duser(self):
        """Eski satırlarda sort_by null olabilir; None iletilmemeli."""
        kwargs = self._kural_motoru_cagrisi(
            [self._rule("top_month", sort_by=None)], "top_month"
        )
        assert kwargs["sort_by"] == "plays"

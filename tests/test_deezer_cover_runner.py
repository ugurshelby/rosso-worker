"""Deezer kapak dolgusu — Spotify'ı olmayan kullanıcıların kapakları (0349)."""
import app.pipeline.deezer_cover_runner as mod
from app.pipeline.deezer_cover_runner import (
    gecerli_kapak,
    run_deezer_cover_backfill,
    sanatci_gorseli,
    track_kapagi,
)
from app.services.genre_errors import RateLimitError

MD5 = "a" * 32
KAPAK = f"https://cdn-images.dzcdn.net/images/cover/{MD5}/500x500-000000-80-0-0.jpg"
ARTIST = f"https://cdn-images.dzcdn.net/images/artist/{MD5}/500x500-000000-80-0-0.jpg"
YER_TUTUCU = "https://cdn-images.dzcdn.net/images/cover//500x500-000000-80-0-0.jpg"


def test_gecerli_kapak_yer_tutucu_ve_yabanci_alan_reddedilir():
    assert gecerli_kapak(KAPAK) == KAPAK
    assert gecerli_kapak(YER_TUTUCU) is None
    assert gecerli_kapak("https://evil.example.com/images/cover/" + MD5 + "/x.jpg") is None
    assert gecerli_kapak(None) is None


def test_track_kapagi_isrc_once(monkeypatch):
    cagrilar = []
    monkeypatch.setattr(mod, "_get_json", lambda http, url: cagrilar.append(url) or {"album": {"cover_big": KAPAK}})
    monkeypatch.setattr(mod, "lookup_track_payload", lambda *a: (_ for _ in ()).throw(AssertionError("aramaya inmemeli")))
    assert track_kapagi({"isrc": "GBUM71505078", "title": "x", "artists": ["y"]}, object()) == KAPAK
    assert cagrilar == ["https://api.deezer.com/track/isrc:GBUM71505078"]


def test_track_kapagi_isrc_yoksa_dogrulanmis_aramaya_duser(monkeypatch):
    monkeypatch.setattr(
        mod, "lookup_track_payload",
        lambda artist, title, http: {"album": {"cover_big": KAPAK}} if (artist, title) == ("Tame Impala", "Let It Happen") else None,
    )
    assert track_kapagi({"isrc": None, "title": "Let It Happen", "artists": ["Tame Impala"]}, object()) == KAPAK
    assert track_kapagi({"isrc": None, "title": "Baska", "artists": ["Tame Impala"]}, object()) is None


def test_track_kapagi_yer_tutucu_kapak_reddedilir(monkeypatch):
    monkeypatch.setattr(mod, "lookup_track_payload", lambda *a: {"album": {"cover_big": YER_TUTUCU}})
    assert track_kapagi({"isrc": None, "title": "A", "artists": ["B"]}, object()) is None


def test_gecersiz_isrc_bicimi_sorgulanmaz(monkeypatch):
    monkeypatch.setattr(mod, "_get_json", lambda *a: (_ for _ in ()).throw(AssertionError("sorgulanmamali")))
    monkeypatch.setattr(mod, "lookup_track_payload", lambda *a: None)
    assert track_kapagi({"isrc": "../etc", "title": "A", "artists": ["B"]}, object()) is None


def test_sanatci_gorseli_ada_tam_eslesme(monkeypatch):
    monkeypatch.setattr(
        mod, "_get_json",
        lambda http, url: {"data": [
            {"name": "Tame Impala Tribute", "picture_big": ARTIST},
            {"name": "Tame Impala", "picture_big": ARTIST},
        ]},
    )
    assert sanatci_gorseli("TAME IMPALA", object()) == ARTIST


def test_sanatci_gorseli_yaklasik_eslesme_kabul_edilmez(monkeypatch):
    monkeypatch.setattr(mod, "_get_json", lambda http, url: {"data": [{"name": "Baska Biri", "picture_big": ARTIST}]})
    assert sanatci_gorseli("Tame Impala", object()) is None


# ── runner ────────────────────────────────────────────────────────────────────

class _Yazim:
    def __init__(self, sink, tablo):
        self._sink, self._tablo = sink, tablo
        self._veri = None
    def update(self, veri):
        self._veri = veri
        return self
    def eq(self, _kolon, deger):
        self._id = deger
        return self
    def execute(self):
        self._sink.append((self._tablo, self._id, self._veri))
        return type("R", (), {"data": []})()


class _Rpc:
    def __init__(self, data):
        self._data = data
    def execute(self):
        return type("R", (), {"data": self._data})()


class _Client:
    def __init__(self, sarkilar, sanatcilar):
        self._r = {"deezer_kapak_adaylari_track": sarkilar, "deezer_kapak_adaylari_sanatci": sanatcilar}
        self.yazilanlar = []
    def rpc(self, ad, _args):
        return _Rpc(self._r[ad])
    def table(self, ad):
        return _Yazim(self.yazilanlar, ad)


def _cooldown_yok(monkeypatch):
    monkeypatch.setattr(mod.cooldown, "is_blocked", lambda c, p: (False, 0))


def test_runner_bulunani_yazar_bulunamayani_yalniz_damgalar(monkeypatch):
    _cooldown_yok(monkeypatch)
    monkeypatch.setattr(mod, "track_kapagi", lambda r, h: KAPAK if r["id"] == "t1" else None)
    monkeypatch.setattr(mod, "sanatci_gorseli", lambda ad, h: ARTIST)
    c = _Client([{"id": "t1"}, {"id": "t2"}], [{"id": "a1", "name": "X"}])
    sonuc = run_deezer_cover_backfill(c, object())

    yazilan = {(t, i): v for t, i, v in c.yazilanlar}
    assert "deezer_image_url" in yazilan[("tracks", "t1")]
    assert "deezer_image_url" not in yazilan[("tracks", "t2")]         # kesin yok → yalnız damga
    assert "deezer_kapak_denendi_at" in yazilan[("tracks", "t2")]
    assert yazilan[("artists", "a1")]["deezer_image_url"] == ARTIST
    assert (sonuc["outcome"], sonuc["updated"], sonuc["skipped"]) == ("success", 2, 1)


def test_runner_hicbir_yerde_image_url_yazmaz(monkeypatch):
    """Öncelik kuralı DB tetikleyicisinde; runner yalnız Deezer alanına yazar."""
    _cooldown_yok(monkeypatch)
    monkeypatch.setattr(mod, "track_kapagi", lambda r, h: KAPAK)
    c = _Client([{"id": "t1"}], [])
    run_deezer_cover_backfill(c, object())
    for _t, _i, veri in c.yazilanlar:
        assert "image_url" not in veri


def test_runner_kota_gecici_hatadir_damga_vurmaz_cooldown_yazar(monkeypatch):
    _cooldown_yok(monkeypatch)
    yazilan_cooldown = []
    monkeypatch.setattr(mod.cooldown, "set_cooldown", lambda c, p, s, reason: yazilan_cooldown.append((p, s, reason)))

    def _patla(r, h):
        raise RateLimitError("deezer", 30.0)

    monkeypatch.setattr(mod, "track_kapagi", _patla)
    c = _Client([{"id": "t1"}], [])
    sonuc = run_deezer_cover_backfill(c, object())

    assert c.yazilanlar == []                       # damga VURULMADI (geçici)
    assert yazilan_cooldown == [("deezer", 30.0, "deezer_cover_429")]
    assert sonuc["outcome"] == "partial" and sonuc["quota_hit"] is True


def test_runner_cooldown_aktifse_hic_istek_atmaz(monkeypatch):
    monkeypatch.setattr(mod.cooldown, "is_blocked", lambda c, p: (True, 500))
    c = _Client([{"id": "t1"}], [])
    sonuc = run_deezer_cover_backfill(c, object())
    assert sonuc["outcome"] == "blocked" and c.yazilanlar == []


def test_runner_kuyruk_bossa_empty(monkeypatch):
    _cooldown_yok(monkeypatch)
    assert run_deezer_cover_backfill(_Client([], []), object())["outcome"] == "empty"


def test_runner_zaman_butcesi_dolarsa_partial(monkeypatch):
    _cooldown_yok(monkeypatch)
    monkeypatch.setattr(mod, "track_kapagi", lambda r, h: KAPAK)
    c = _Client([{"id": "t1"}, {"id": "t2"}], [])
    sonuc = run_deezer_cover_backfill(c, object(), time_budget_s=-1.0)
    assert sonuc["outcome"] == "partial" and sonuc["processed"] == 0

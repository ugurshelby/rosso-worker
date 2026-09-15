"""small_artist_backfill — az-track sanatçı backfill testleri (2026-07-16).

Ayrıca Katman 1 (normalize Deezer kapsaması) ve Katman 2 (id_only profil
güvenlik sınırları) testleri de burada — üç katman tek işin parçası.
"""
import pytest

from app.pipeline import small_artist_backfill as bf
from app.services import artist_profile
from app.services.deezer_genre import _DEEZER_GENRE_MAP
from app.services.genre_normalize import canonical_for


# ─────────────────────────────────────────────────────────────────────
# Katman 1 — normalize haritası Deezer'ın TÜM etiketlerini kapsamalı
# ─────────────────────────────────────────────────────────────────────
def test_deezer_genre_map_tamamen_normalize_kapsaminda():
    """Deezer'ın kendi genre_id etiketlerinden hiçbiri normalize'da boşa düşmemeli.

    2026-07-16 canlı bulgu: 'Dans' haritada yoktu → CURSEDEVIL track verisi
    bulunup boşa düşüyordu, sanatçı haksız yere pending kalıyordu.
    """
    missing = [label for label in _DEEZER_GENRE_MAP.values() if not canonical_for(label)]
    assert missing == [], f"Haritasız Deezer etiketi: {missing}"


def test_deezer_ingilizce_yuzey_etiketleri_de_kapsanir():
    """Deezer album endpoint'i EN de dönebilir — iki dil de eşlenmiş olmalı."""
    en_labels = ["Dance", "Electro", "Films/Games", "Latin Music", "African Music",
                 "Asian Music", "Brazilian Music", "Indian Music", "Soul & Funk",
                 "Arabic Music"]
    missing = [label for label in en_labels if not canonical_for(label)]
    assert missing == [], f"Haritasız EN etiketi: {missing}"


def test_dance_elektronik_kokunde():
    assert canonical_for("Dans") == "dance"
    from app.services.genre_normalize import GENRE_HIERARCHY
    assert GENRE_HIERARCHY["dance"]["parent"] == "elektronik"


# ─────────────────────────────────────────────────────────────────────
# Katman 2 — id_only profil güvenlik sınırları
# ─────────────────────────────────────────────────────────────────────
class _NoTableClient:
    """table() çağrılırsa patlar — 'cache'e dokunmadı' kanıtı."""
    def table(self, *a, **k):
        raise AssertionError("id_only + id'siz çağrı DB'ye dokunmamalı")


def test_id_only_query_id_yoksa_bos_doner_ve_cache_kirletmez():
    result = artist_profile.get_or_build_artist_profile(
        "Test Artist", _NoTableClient(), http=None, lastfm_key="",
        id_only=True,  # query_id YOK
    )
    assert result["slots"] == []


def test_id_only_lastfm_ve_mb_isim_aramalarini_atlar(monkeypatch):
    """id_only=True iken Last.fm/MB isim aramaları HİÇ çağrılmamalı —
    az-track sanatçıda güven hakemi çalışamaz, koruma tam bunun için var."""
    def _bomb(*a, **k):
        raise AssertionError("id_only yolda isim araması yapılmamalı")
    monkeypatch.setattr(artist_profile, "get_lastfm_artist_tags_scored", _bomb)
    monkeypatch.setattr(artist_profile, "get_musicbrainz_artist_genres", _bomb)
    monkeypatch.setattr(artist_profile, "get_db_artist_genres", lambda *a, **k: [])
    monkeypatch.setattr(
        artist_profile, "get_deezer_artist_genres_by_id",
        lambda artist_id, http: [("Rap/Hip Hop", 10)],
    )
    profile = artist_profile._build_profile(
        "Küçük Sanatçı", client=None, http=None, lastfm_key="x",
        query_id=12345, id_only=True,
    )
    assert profile["slots"] == ["hip-hop"]
    assert profile["sources"]["hip-hop"] == ["deezer_artist"]


# ─────────────────────────────────────────────────────────────────────
# Katman 3 — backfill runner davranışı (fake client)
# ─────────────────────────────────────────────────────────────────────
class _FakeQuery:
    def __init__(self, client, table_name):
        self._client = client
        self._table = table_name
        self._payload = None
        self._eq = {}
    def select(self, *a, **k): return self
    def eq(self, col, val):
        self._eq[col] = val
        return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def update(self, payload):
        self._payload = payload
        return self
    def execute(self):
        if self._payload is not None:  # update
            self._client.updates.append((self._eq.get("id"), self._payload))
            return type("R", (), {"data": []})()
        # select: pending rows döndür
        return type("R", (), {"data": self._client.pending_rows})()


class _FakeClient:
    def __init__(self, pending_rows):
        self.pending_rows = pending_rows
        self.updates = []
    def table(self, name):
        return _FakeQuery(self, name)
    def rpc(self, name, params):
        class _Q:
            def execute(self_):
                return type("R", (), {"data": []})()  # cooldown yok
        return _Q()


@pytest.fixture()
def _no_cooldown(monkeypatch):
    monkeypatch.setattr(bf.cooldown, "is_blocked", lambda c, p: (False, 0))


def test_backfill_track_verisi_bulunursa_yazar_ve_pending_temizler(monkeypatch, _no_cooldown):
    client = _FakeClient([
        {"id": "t1", "title": "Şarkı A", "artists": ["Sanatçı X"]},
    ])
    monkeypatch.setattr(
        bf, "build_track_genre_data",
        lambda a, t, lastfm_key, http: (
            {"slots": ["dance"], "weights": [0.6], "raw_scores": {}, "sources": {}},
            "Sanatçı X", 777, None,
        ),
    )
    monkeypatch.setattr(
        bf, "get_or_build_artist_profile",
        lambda *a, **k: {"slots": ["dance"], "weights": [0.4]},
    )
    result = bf.run_small_artist_backfill(client, settings=type("S", (), {})())
    assert result["outcome"] == "success"
    assert result["updated"] == 1
    track_id, payload = client.updates[0]
    assert track_id == "t1"
    assert payload["genres"] == ["dance"]
    assert payload["genre_pending_reason"] is None  # pending TEMİZLENDİ
    assert payload["genre_source"] == "multi_source"


def test_backfill_hicbir_sey_bulamazsa_no_match_isaretler_lookup_failed_degil(
    monkeypatch, _no_cooldown
):
    client = _FakeClient([
        {"id": "t2", "title": "Bulunamayan", "artists": ["Meçhul"]},
    ])
    monkeypatch.setattr(
        bf, "build_track_genre_data",
        lambda a, t, lastfm_key, http: (
            {"slots": [], "weights": [], "raw_scores": {}, "sources": {}}, None, None, None,
        ),
    )
    result = bf.run_small_artist_backfill(client, settings=type("S", (), {})())
    assert result["no_match"] == 1
    _, payload = client.updates[0]
    assert payload == {"genre_pending_reason": bf.NO_MATCH_REASON}
    # lookup_failed YAZILMADI — sanatçı büyüyünce RPC işareti temizler
    assert "genre_lookup_failed_at" not in payload


def test_backfill_429da_cooldown_yazar_ve_kismi_cikar(monkeypatch, _no_cooldown):
    client = _FakeClient([
        {"id": "t3", "title": "Rate Limited", "artists": ["X"]},
    ])
    monkeypatch.setattr(
        bf, "build_track_genre_data",
        lambda a, t, lastfm_key, http: (
            {"slots": [], "weights": [], "raw_scores": {}, "sources": {}},
            None, None, ("deezer", 30.0),
        ),
    )
    calls = []
    monkeypatch.setattr(
        bf.cooldown, "set_cooldown",
        lambda client, provider, ra, reason: calls.append((provider, ra, reason)) or 30,
    )
    result = bf.run_small_artist_backfill(client, settings=type("S", (), {})())
    assert result["outcome"] == "rate_limited"
    assert calls == [("deezer", 30.0, "genre_429")]
    assert client.updates == []  # hiçbir track'e dokunulmadı


def test_backfill_deezer_blokluysa_hic_baslamaz(monkeypatch):
    monkeypatch.setattr(bf.cooldown, "is_blocked", lambda c, p: (True, 120))
    client = _FakeClient([{"id": "t4", "title": "x", "artists": ["Y"]}])
    result = bf.run_small_artist_backfill(client, settings=type("S", (), {})())
    assert result["outcome"] == "blocked"
    assert client.updates == []

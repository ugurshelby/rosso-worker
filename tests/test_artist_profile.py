"""artist_profile — 4 kaynaktan ağırlıklı sanatçı DNA profili + 30 gün cache testleri."""
from datetime import datetime, timedelta, timezone

import pytest

import app.services.artist_profile as ap
from app.services.artist_profile import (
    is_artist_match,
    normalize_artist_name,
    get_or_build_artist_profile,
)


# ─────────────────────────────────────────────────────────────────────
# is_artist_match — Deezer fallback isim doğrulaması (difflib)
# ─────────────────────────────────────────────────────────────────────
def test_isim_eslesme_ayni():
    assert is_artist_match("Drake", "Drake") is True
    assert is_artist_match("SALİ", "sali") is True  # case-insensitive


def test_isim_eslesme_yanlis_kisi():
    # Dry run tespiti: Drake → Brezilyalı farklı sanatçı, SALİ → Saliva
    assert is_artist_match("Drake", "Drake Milligan") is False
    assert is_artist_match("SALİ", "Saliva") is False


def test_normalize_artist_name():
    assert normalize_artist_name("  Drake ") == "drake"
    assert normalize_artist_name("SALİ") == "sali̇" or normalize_artist_name("SALİ") == "sali"


# ─────────────────────────────────────────────────────────────────────
# Cache: taze profil varsa API'ye gitme
# ─────────────────────────────────────────────────────────────────────
class _Query:
    """Zincirlenebilir sahte Supabase query."""
    def __init__(self, table):
        self._table = table

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        return type("R", (), {"data": self._table["select_data"]})()

    def insert(self, row):
        self._table["inserted"].append(row)
        return self

    def update(self, row):
        self._table["updated"].append(row)
        return self


class _Client:
    def __init__(self, select_data=None):
        self.state = {"select_data": select_data or [], "inserted": [], "updated": []}

    def table(self, name):
        return _Query(self.state)


def test_cache_taze_profil_api_cagirmaz(monkeypatch):
    fresh = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    cached = {
        "name_normalized": "drake",
        "genre_data": {"slots": ["hip-hop"], "weights": [0.40]},
        "genres": ["hip-hop"],
        "refreshed_at": fresh,
        "genre_lookup_failed_at": None,
    }
    client = _Client(select_data=[cached])

    # Herhangi bir dış kaynak çağrılırsa test patlasın
    def _boom(*a, **k):
        raise AssertionError("Taze cache varken dış kaynak çağrılmamalı")

    monkeypatch.setattr(ap, "get_lastfm_artist_tags_scored", _boom)
    monkeypatch.setattr(ap, "get_musicbrainz_artist_genres", _boom)

    profile = get_or_build_artist_profile("Drake", client, http=None, lastfm_key="k")
    assert profile["slots"] == ["hip-hop"]


def test_cache_bayat_profil_yeniden_insa(monkeypatch):
    stale = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    cached = {
        "name_normalized": "drake",
        "genre_data": {"slots": ["eski"], "weights": [0.40]},
        "refreshed_at": stale,
        "genre_lookup_failed_at": None,
    }
    client = _Client(select_data=[cached])

    monkeypatch.setattr(ap, "get_lastfm_artist_tags_scored",
                        lambda *a, **k: [("hip hop", 100)])
    monkeypatch.setattr(ap, "get_musicbrainz_artist_genres",
                        lambda *a, **k: [("rap", 50)])
    monkeypatch.setattr(ap, "get_deezer_artist_genres_scored", lambda *a, **k: ([], None))
    monkeypatch.setattr(ap, "get_db_artist_genres", lambda *a, **k: [])

    profile = get_or_build_artist_profile("Drake", client, http=object(), lastfm_key="k")
    # Bayat → yeniden inşa → yeni türler
    assert "hip-hop" in profile["slots"]
    # DB'ye güncelleme yazıldı
    assert len(client.state["updated"]) == 1


def test_az_trackli_sanatci_profil_kurmaz(monkeypatch):
    """<5 track'li sanatçıda artist profili KURULMAZ (isim çakışması koruması,
    2026-07-05). Mahmut Tuncer/Yıldız Tilbe gibi tek-track sanatçıların yanlış
    'black metal' profili almasını önler. Boş döner, dış kaynak hiç çağrılmaz."""
    client = _Client(select_data=[])  # cache yok

    def _boom(*a, **k):
        raise AssertionError("Az-track'li sanatçıda dış kaynak çağrılmamalı")

    monkeypatch.setattr(ap, "get_lastfm_artist_tags_scored", _boom)
    monkeypatch.setattr(ap, "get_musicbrainz_artist_genres", _boom)
    # Sanatçının DB'de yalnızca 1 track'i var (eşik altı)
    monkeypatch.setattr(ap, "get_db_artist_track_count", lambda *a, **k: 1)

    profile = get_or_build_artist_profile(
        "Mahmut Tuncer", client, http=object(), lastfm_key="k", min_tracks=5
    )
    assert profile["slots"] == []
    # Cache'e yazılmamalı (ne insert ne update) — ileride track sayısı artınca denenir
    assert len(client.state["inserted"]) == 0
    assert len(client.state["updated"]) == 0


def test_yeterli_trackli_sanatci_profil_kurar(monkeypatch):
    """>=5 track'li sanatçıda profil normal kurulur (eşik geçildi)."""
    client = _Client(select_data=[])

    monkeypatch.setattr(ap, "get_lastfm_artist_tags_scored",
                        lambda *a, **k: [("hip hop", 100)])
    monkeypatch.setattr(ap, "get_musicbrainz_artist_genres", lambda *a, **k: [])
    monkeypatch.setattr(ap, "get_deezer_artist_genres_scored", lambda *a, **k: ([], None))
    monkeypatch.setattr(ap, "get_db_artist_genres", lambda *a, **k: [])
    monkeypatch.setattr(ap, "get_db_artist_track_count", lambda *a, **k: 8)

    profile = get_or_build_artist_profile(
        "Ceza", client, http=object(), lastfm_key="k", min_tracks=5
    )
    assert "hip-hop" in profile["slots"]


def test_yeni_sanatci_insa_ve_insert(monkeypatch):
    client = _Client(select_data=[])  # cache yok
    # Bu test min_tracks kontrolünü atlatmalı (varsayılan min_tracks=0 ile geriye uyum)
    monkeypatch.setattr(ap, "get_db_artist_track_count", lambda *a, **k: 10)

    monkeypatch.setattr(ap, "get_lastfm_artist_tags_scored",
                        lambda *a, **k: [("hip hop", 100), ("r&b", 60)])
    monkeypatch.setattr(ap, "get_musicbrainz_artist_genres",
                        lambda *a, **k: [("rap", 50)])
    monkeypatch.setattr(ap, "get_deezer_artist_genres_scored", lambda *a, **k: ([], None))
    monkeypatch.setattr(ap, "get_db_artist_genres", lambda *a, **k: [])

    profile = get_or_build_artist_profile("Drake", client, http=object(), lastfm_key="k")
    assert "hip-hop" in profile["slots"]
    assert len(profile["slots"]) <= 5
    assert len(client.state["inserted"]) == 1


def test_tum_kaynaklar_bos_lookup_failed(monkeypatch):
    client = _Client(select_data=[])

    monkeypatch.setattr(ap, "get_lastfm_artist_tags_scored", lambda *a, **k: [])
    monkeypatch.setattr(ap, "get_musicbrainz_artist_genres", lambda *a, **k: [])
    monkeypatch.setattr(ap, "get_deezer_artist_genres_scored", lambda *a, **k: ([], None))
    monkeypatch.setattr(ap, "get_db_artist_genres", lambda *a, **k: [])

    profile = get_or_build_artist_profile("Nobody", client, http=object(), lastfm_key="k")
    assert profile["slots"] == []
    # lookup_failed_at içeren bir kayıt insert edildi
    assert len(client.state["inserted"]) == 1
    assert client.state["inserted"][0].get("genre_lookup_failed_at") is not None


def test_deezer_fallback_isim_dogrulamasi(monkeypatch):
    """Deezer yanlış sanatçı döndürürse (isim eşleşmezse) o kaynak kullanılmaz."""
    client = _Client(select_data=[])

    monkeypatch.setattr(ap, "get_lastfm_artist_tags_scored", lambda *a, **k: [])
    monkeypatch.setattr(ap, "get_musicbrainz_artist_genres", lambda *a, **k: [])
    monkeypatch.setattr(ap, "get_db_artist_genres", lambda *a, **k: [])
    # Deezer "brezilya müziği" döndürür AMA bulunan isim eşleşmez → elenmeli
    monkeypatch.setattr(ap, "get_deezer_artist_genres_scored",
                        lambda *a, **k: ([("brezilya müziği", 50)], "Drake Brasil"))

    profile = get_or_build_artist_profile("Drake", client, http=object(), lastfm_key="k")
    # Yanlış eşleşme elendiği için profil boş
    assert profile["slots"] == []


def test_build_profile_uses_query_name_for_lastfm_mb(monkeypatch):
    """query_name verilince Last.fm/MB sorguları çapa adıyla yapılır; db_tracks artists[0] ile."""
    from app.services import artist_profile

    seen = {}

    def fake_lastfm(name, key, http):
        seen["lastfm"] = name
        return [("rap", 80)]

    def fake_mb(name, http):
        seen["mb"] = name
        return [("hip hop", 50)]

    def fake_db(name, client):
        seen["db"] = name
        return []

    def fake_deezer(name, http):
        seen["deezer"] = name
        return [], None

    monkeypatch.setattr(artist_profile, "get_lastfm_artist_tags_scored", fake_lastfm)
    monkeypatch.setattr(artist_profile, "get_musicbrainz_artist_genres", fake_mb)
    monkeypatch.setattr(artist_profile, "get_db_artist_genres", fake_db)
    monkeypatch.setattr(artist_profile, "get_deezer_artist_genres_scored", fake_deezer)

    artist_profile._build_profile("Yung Ouzo", None, None, "key", query_name="Tanerman")

    # Last.fm / MB / Deezer çapa adıyla; db_tracks DB adıyla
    assert seen["lastfm"] == "Tanerman"
    assert seen["mb"] == "Tanerman"
    assert seen["deezer"] == "Tanerman"
    assert seen["db"] == "Yung Ouzo"


def test_build_profile_query_id_resolves_deezer_by_id(monkeypatch):
    """query_id verilince Deezer artist DNA'sı isimle ARAMADAN doğrudan id'den
    çözülür (2026-07-04). Deezer isim araması yanlış sanatçı buluyor (Motive→Harp).
    Track çapasının doğrulanmış artist.id'si isim çakışmasını tamamen ortadan kaldırır."""
    from app.services import artist_profile

    seen = {}

    def fake_by_id(artist_id, http):
        seen["by_id"] = artist_id
        return [("Rap/Hip Hop", 42)]

    def fake_by_name(name, http):
        seen["by_name"] = name  # ÇAĞRILMAMALI
        return [], None

    monkeypatch.setattr(artist_profile, "get_lastfm_artist_tags_scored", lambda *a, **k: [])
    monkeypatch.setattr(artist_profile, "get_musicbrainz_artist_genres", lambda *a, **k: [])
    monkeypatch.setattr(artist_profile, "get_db_artist_genres", lambda *a, **k: [])
    monkeypatch.setattr(artist_profile, "get_deezer_artist_genres_by_id", fake_by_id)
    monkeypatch.setattr(artist_profile, "get_deezer_artist_genres_scored", fake_by_name)

    profile = artist_profile._build_profile(
        "Motive", None, object(), "key", query_name="Motive", query_id=72196
    )

    assert seen["by_id"] == 72196           # id'den çözüldü
    assert "by_name" not in seen            # isimle arama YAPILMADI
    assert "hip-hop" in profile["slots"]    # doğru tür


def test_build_profile_no_query_id_falls_back_to_name(monkeypatch):
    """query_id yoksa (geriye uyum) Deezer isimle aranır (eski davranış korunur)."""
    from app.services import artist_profile

    seen = {}
    monkeypatch.setattr(artist_profile, "get_lastfm_artist_tags_scored", lambda *a, **k: [])
    monkeypatch.setattr(artist_profile, "get_musicbrainz_artist_genres", lambda *a, **k: [])
    monkeypatch.setattr(artist_profile, "get_db_artist_genres", lambda *a, **k: [])
    monkeypatch.setattr(
        artist_profile, "get_deezer_artist_genres_scored",
        lambda name, http: (seen.update(by_name=name) or ([], None)),
    )

    artist_profile._build_profile("Ceza", None, object(), "key")
    assert seen["by_name"] == "Ceza"        # id yok → isimle arama


# ─────────────────────────────────────────────────────────────────────
# resolve_lastfm_trust — Last.fm artist güven kuralı (BÖLÜM 2, Fable 5 2026-07-03)
# ─────────────────────────────────────────────────────────────────────
def test_trust_bos_slots_empty():
    from app.services.artist_profile import resolve_lastfm_trust
    status, kept = resolve_lastfm_trust([], [])
    assert status == "EMPTY"
    assert kept == []


def test_trust_tek_tur_trusted():
    """Tek tür → koşulsuz TRUSTED (Şehinşah, Ceza deseni)."""
    from app.services.artist_profile import resolve_lastfm_trust
    status, kept = resolve_lastfm_trust(["hip-hop"], [])
    assert status == "TRUSTED"
    assert kept == ["hip-hop"]


def test_trust_cok_tur_yuksek_tutarlilik_trusted():
    """Çok tür ama ikili akrabalık ≥%50 → TRUSTED (CLANN 100 deseni).
    hip-hop + r&b + trap → hepsi akraba."""
    from app.services.artist_profile import resolve_lastfm_trust
    status, kept = resolve_lastfm_trust(["hip-hop", "trap", "r&b"], [])
    assert status == "TRUSTED"
    assert set(kept) == {"hip-hop", "trap", "r&b"}


def test_trust_cakisma_az_db_suspect():
    """Çakışma (%0 tutarlılık) + DB'de <5 track → SUSPECT (Azer Bülbül deseni:
    arabesk + black metal, DB'de 2 track → dokunma)."""
    from app.services.artist_profile import resolve_lastfm_trust
    status, kept = resolve_lastfm_trust(["arabesk", "black metal"], ["arabesk", "arabesk"])
    assert status == "SUSPECT"
    assert kept == []


def test_trust_cakisma_yeterli_db_daraltilmis_trusted():
    """Çakışma + DB'de ≥5 track + DB çoğunluğu lastfm slot'larından biriyle akraba
    → daraltılmış TRUSTED (Motive deseni: DB=hip-hop, lastfm=hip-hop+metalcore →
    metalcore atılır, hip-hop kalır)."""
    from app.services.artist_profile import resolve_lastfm_trust
    db = ["hip-hop"] * 6  # DB çoğunluğu hip-hop, ≥5 track
    status, kept = resolve_lastfm_trust(["hip-hop", "metalcore"], db)
    assert status == "TRUSTED"
    assert "hip-hop" in kept
    assert "metalcore" not in kept  # yabancı slot atıldı


def test_trust_cakisma_yeterli_db_ama_akraba_degil_suspect():
    """Çakışma + DB'de ≥5 track AMA DB çoğunluğu lastfm slot'larıyla akraba değil
    → SUSPECT (profil zehirlenmesin)."""
    from app.services.artist_profile import resolve_lastfm_trust
    db = ["klasik"] * 6  # DB çoğunluğu klasik, lastfm slot'larıyla akraba değil
    status, kept = resolve_lastfm_trust(["death metal", "pop"], db)
    assert status == "SUSPECT"
    assert kept == []


def test_lookup_failed_retry_after_7_days(monkeypatch):
    """lookup_failed sanatçı 7 günden eskiyse YENİDEN denenir (kalıcı kilit yok).
    5 gün önce başarısız olmuş → henüz denenmez; 10 gün önce → yeniden dener."""
    from app.services import artist_profile

    # 10 gün önce lookup_failed olmuş, refreshed_at da o zamandan
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    cached = {
        "name_normalized": "yeniden",
        "genre_data": None,
        "genres": None,
        "refreshed_at": old,
        "genre_lookup_failed_at": old,
    }
    client = _Client(select_data=[cached])

    called = {"lastfm": False}
    def fake_lastfm(*a, **k):
        called["lastfm"] = True
        return [("trap", 100)]
    monkeypatch.setattr(artist_profile, "get_lastfm_artist_tags_scored", fake_lastfm)
    monkeypatch.setattr(artist_profile, "get_musicbrainz_artist_genres", lambda *a, **k: [])
    monkeypatch.setattr(artist_profile, "get_db_artist_genres", lambda *a, **k: [])
    monkeypatch.setattr(artist_profile, "get_deezer_artist_genres_scored",
                        lambda *a, **k: ([], None))

    profile = artist_profile.get_or_build_artist_profile(
        "Yeniden", client, http=object(), lastfm_key="k"
    )
    # 7 günü geçmiş lookup_failed → yeniden denendi ve bu sefer bulundu
    assert called["lastfm"] is True
    assert "trap" in profile["slots"]


def test_lookup_failed_not_retried_before_7_days(monkeypatch):
    """5 gün önce lookup_failed olmuş → henüz yeniden DENENMEZ (dış kaynak çağrılmaz)."""
    from app.services import artist_profile

    recent = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    cached = {
        "name_normalized": "bekle",
        "genre_data": None,
        "genres": None,
        "refreshed_at": recent,
        "genre_lookup_failed_at": recent,
    }
    client = _Client(select_data=[cached])

    def _boom(*a, **k):
        raise AssertionError("7 gün dolmadan yeniden denenmemeli")
    monkeypatch.setattr(artist_profile, "get_lastfm_artist_tags_scored", _boom)
    monkeypatch.setattr(artist_profile, "get_musicbrainz_artist_genres", _boom)

    profile = artist_profile.get_or_build_artist_profile(
        "Bekle", client, http=object(), lastfm_key="k"
    )
    assert profile["slots"] == []


def test_build_profile_suspect_lastfm_excluded(monkeypatch):
    """Last.fm SUSPECT ise (çakışma + yetersiz DB) merge'e girmez; diğer
    kaynaklar profili belirler (Motive'i köre güvenden koruma)."""
    from app.services import artist_profile

    # Last.fm çelişkili (death metal + pop, akraba değil), DB yetersiz
    monkeypatch.setattr(artist_profile, "get_lastfm_artist_tags_scored",
                        lambda *a, **k: [("death metal", 100), ("pop", 90)])
    monkeypatch.setattr(artist_profile, "get_musicbrainz_artist_genres",
                        lambda *a, **k: [("hip hop", 50)])
    monkeypatch.setattr(artist_profile, "get_db_artist_genres", lambda *a, **k: [])
    monkeypatch.setattr(artist_profile, "get_deezer_artist_genres_scored",
                        lambda *a, **k: ([], None))

    profile = artist_profile._build_profile("Motive", None, object(), "key")
    # Last.fm SUSPECT → death metal/pop girmez; MB'den hip-hop kalır
    assert "hip-hop" in profile["slots"]
    assert "death metal" not in profile["slots"]


def test_build_profile_trusted_lastfm_included(monkeypatch):
    """Last.fm TRUSTED (tek tür) ise merge'e girer."""
    from app.services import artist_profile

    monkeypatch.setattr(artist_profile, "get_lastfm_artist_tags_scored",
                        lambda *a, **k: [("trap", 100)])
    monkeypatch.setattr(artist_profile, "get_musicbrainz_artist_genres", lambda *a, **k: [])
    monkeypatch.setattr(artist_profile, "get_db_artist_genres", lambda *a, **k: [])
    monkeypatch.setattr(artist_profile, "get_deezer_artist_genres_scored",
                        lambda *a, **k: ([], None))

    profile = artist_profile._build_profile("Şehinşah", None, object(), "key")
    assert "trap" in profile["slots"]


def test_build_profile_without_query_name_uses_artist(monkeypatch):
    """query_name yoksa (geriye uyum) tüm sorgular artist adıyla."""
    from app.services import artist_profile

    seen = {}
    monkeypatch.setattr(artist_profile, "get_lastfm_artist_tags_scored",
                        lambda n, k, h: seen.update(lastfm=n) or [])
    monkeypatch.setattr(artist_profile, "get_musicbrainz_artist_genres",
                        lambda n, h: seen.update(mb=n) or [])
    monkeypatch.setattr(artist_profile, "get_db_artist_genres",
                        lambda n, c: seen.update(db=n) or [])
    monkeypatch.setattr(artist_profile, "get_deezer_artist_genres_scored",
                        lambda n, h: seen.update(deezer=n) or ([], None))

    artist_profile._build_profile("manifest", None, None, "key")

    assert seen["lastfm"] == "manifest"
    assert seen["mb"] == "manifest"
    assert seen["deezer"] == "manifest"

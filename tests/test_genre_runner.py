"""genre_runner — genre DNA enrichment testleri (çok kaynak, ağırlıklı, genre_data)."""
from datetime import datetime, timedelta, timezone

import pytest

from app.pipeline import genre_runner


@pytest.fixture(autouse=True)
def _default_artist_track_count(monkeypatch):
    """Varsayılan: sanatçı eşik ÜSTÜ (≥5 track) sayılır — isim-çakışması guard'ı
    (2026-07-05) çoğu testte tetiklenmez. Az-track senaryosunu test eden testler
    bu mock'u kendi içinde override eder. get_db_artist_track_count gerçek DB'ye
    gitmesin diye burada sabitlenir."""
    monkeypatch.setattr(genre_runner, "get_db_artist_track_count", lambda *a, **k: 99)


# ─────────────────────────────────────────────────────────────────────
# Fake Supabase client — tracks batch + update izleme
# ─────────────────────────────────────────────────────────────────────
class _Query:
    def __init__(self, rows):
        self._rows = rows
    @property
    def not_(self):
        return self
    def select(self, *a, **k): return self
    def is_(self, *a, **k): return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def execute(self):
        return type("R", (), {"data": self._rows})()


class _CooldownClient:
    def __init__(self, cooldown_rows, track_rows=None):
        self._cooldown_rows = cooldown_rows
        self._track_rows = track_rows or []
    def rpc(self, name, params):
        rows = self._cooldown_rows if name == "cooldown_get" else None
        return _Query(rows)
    def table(self, *a, **k):
        return _Query(self._track_rows)


class _WriteClient:
    """Track update'lerini izler: genre_data + genres yazımı, failed_at işaretleme,
    geri kontrol (contains(artists) ile failed temizleme)."""
    def __init__(self, track_rows):
        self._track_rows = track_rows
        self.written: dict[str, dict] = {}     # track_id → update payload
        self.failed_ids: list[str] = []
        self.pending_ids: list[str] = []       # genre_pending_reason yazılan track'ler
        self.recheck_cleared: list[str] = []   # geri kontrolle temizlenen sanatçılar

    def rpc(self, name, params):
        class _Q:
            def execute(self_):
                return type("R", (), {"data": []})()
        return _Q()

    def table(self, name):
        client = self
        class _Q:
            def __init__(self):
                self._payload = None
                self._eq_val = None
                self._contains_val = None
            def select(self, *a, **k): return self
            def is_(self, *a, **k): return self
            @property
            def not_(self): return self
            def order(self, *a, **k): return self
            def limit(self, *a, **k): return self
            def contains(self, col, val):
                self._contains_val = val
                return self
            def update(self, payload):
                self._payload = payload
                return self
            def eq(self, col, val):
                self._eq_val = val
                return self
            def execute(self):
                # Geri kontrol: update(failed=None) + contains(artists, [X])
                if (self._payload and "genre_lookup_failed_at" in self._payload
                        and self._payload.get("genre_lookup_failed_at") is None
                        and self._contains_val):
                    client.recheck_cleared.append(self._contains_val[0])
                elif self._payload and self._eq_val:
                    if "genre_lookup_failed_at" in self._payload:
                        client.failed_ids.append(self._eq_val)
                    elif "genre_pending_reason" in self._payload:
                        client.pending_ids.append(self._eq_val)
                    else:
                        client.written[self._eq_val] = self._payload
                return type("R", (), {"data": client._track_rows})()
        return _Q()


def _settings(lastfm="lfkey"):
    return type("S", (), {"lastfm_api_key": lastfm})()


# ─────────────────────────────────────────────────────────────────────
# Cooldown / boş batch kapıları (korunan mantık)
# ─────────────────────────────────────────────────────────────────────
def test_blocked_returns_early():
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    client = _CooldownClient(cooldown_rows=[{"blocked_until": future, "reason": "429", "hit_count": 1}])
    result = genre_runner.run_one_genre_batch(client, _settings())
    assert result["outcome"] == "blocked"


def test_empty_when_no_pending():
    client = _CooldownClient(cooldown_rows=[], track_rows=[])
    result = genre_runner.run_one_genre_batch(client, _settings())
    assert result["outcome"] == "empty"


# ─────────────────────────────────────────────────────────────────────
# build_track_genre_data — çok kaynak → genre_data (3 slot)
# ─────────────────────────────────────────────────────────────────────
def test_track_genre_data_sadece_deezer(monkeypatch):
    """Track-seviyesi artık SADECE Deezer (2026-07-03, BÖLÜM 3): Last.fm track
    (0/21 ölü) ve MB recording (4/21, 0 tag, 1sn/track) akıştan çıkarıldı.
    Deezer sonucu tek başına slot'ları belirler."""
    monkeypatch.setattr(genre_runner, "get_deezer_track_scored_with_anchor",
                        lambda a, t, http: ([("Rap/Hip Hop", 0), ("Pop", 0)], "Ezhel", 111))

    data, anchor, _, _ = genre_runner.build_track_genre_data("Ezhel", "Geceler", lastfm_key="k", http=None)
    assert "hip-hop" in data["slots"]  # Deezer 'Rap/Hip Hop' → hip-hop
    assert "pop" in data["slots"]
    assert len(data["slots"]) <= 3
    assert data["weights"][0] == 0.60
    assert anchor == "Ezhel"


def test_track_genre_data_lastfm_mb_import_edilmez():
    """Last.fm track + MB recording fonksiyonları genre_runner'a artık import
    EDİLMEZ (akıştan çıkarıldı, throughput/rate-limit kazanımı). Modülde bu
    isimlerin bulunmaması, track-seviyesinde çağrılamayacaklarının kanıtıdır."""
    assert not hasattr(genre_runner, "get_lastfm_track_tags_scored")
    assert not hasattr(genre_runner, "get_musicbrainz_recording_genres")


def test_track_genre_data_bos_tum_kaynaklar(monkeypatch):
    monkeypatch.setattr(genre_runner, "get_deezer_track_scored_with_anchor",
                        lambda a, t, http: ([], None, None))

    data, anchor, _, _ = genre_runner.build_track_genre_data("X", "Y", lastfm_key="k", http=None)
    assert data["slots"] == []
    assert anchor is None


def test_track_genre_data_bos_artist():
    data, anchor, _, _ = genre_runner.build_track_genre_data("", "Y", lastfm_key="k", http=None)
    assert data["slots"] == []
    assert anchor is None


# ─────────────────────────────────────────────────────────────────────
# Boş slot doldurma: artist profilinden
# ─────────────────────────────────────────────────────────────────────
def test_bos_slot_aile_uyumlu_artist_turu_ile_dolar():
    # Yung Ouzo: track hip-hop buldu. Artist drill/trap (hip-hop ailesi) → eklenir.
    track_data = {"slots": ["hip-hop"], "weights": [0.60],
                  "raw_scores": {"hip-hop": 100}, "sources": {"hip-hop": ["deezer_track"]}}
    artist_profile = {"slots": ["hip-hop", "drill", "trap"], "weights": [0.40, 0.25, 0.15]}

    filled = genre_runner.fill_empty_slots(track_data, artist_profile)
    assert "hip-hop" in filled["slots"]
    assert "drill" in filled["slots"]  # hip-hop ailesi → eklendi
    assert "trap" in filled["slots"]   # hip-hop ailesi → eklendi
    assert len(filled["slots"]) == len(set(filled["slots"]))


def test_bos_slot_alakasiz_artist_turu_elenir():
    # manifest bug'ı: track pop + artist death metal (metal ailesi) → elenir.
    track_data = {"slots": ["pop"], "weights": [0.60],
                  "raw_scores": {"pop": 45}, "sources": {"pop": ["deezer_track"]}}
    artist_profile = {"slots": ["death metal", "drum and bass"], "weights": [0.40, 0.25]}

    filled = genre_runner.fill_empty_slots(track_data, artist_profile)
    assert filled["slots"] == ["pop"]  # death metal pop ile akraba değil → elendi


def test_bos_slot_cross_genre_her_zaman_eklenir():
    # instrumental cross-genre → track hip-hop ile de akraba
    track_data = {"slots": ["hip-hop"], "weights": [0.60],
                  "raw_scores": {"hip-hop": 100}, "sources": {"hip-hop": ["deezer_track"]}}
    artist_profile = {"slots": ["instrumental"], "weights": [0.40]}

    filled = genre_runner.fill_empty_slots(track_data, artist_profile)
    assert "instrumental" in filled["slots"]


def test_bos_slot_referans_yoksa_doldurma_yok():
    # Track tamamen boş → aile referansı yok → fill_empty_slots doldurmaz.
    # (Boş track için Deezer-taban fallback ayrı mekanizma, run_batch'te.)
    track_data = {"slots": [], "weights": [], "raw_scores": {}, "sources": {}}
    artist_profile = {"slots": ["death metal", "pop"], "weights": [0.40, 0.25]}

    filled = genre_runner.fill_empty_slots(track_data, artist_profile)
    assert filled["slots"] == []


# ─────────────────────────────────────────────────────────────────────
# _build_from_artist_base — boş track için güvenilir taban + aile-süzme
# ─────────────────────────────────────────────────────────────────────
def test_artist_base_taban_sources_tan_bulunur():
    # Yung Ouzo senaryosu: hiyerarşi filtresi hip-hop'u slots'tan attı ama
    # sources'ta hip-hop/deezer_artist DURUYOR. Taban buradan bulunmalı.
    profile = {
        "slots": ["drill", "trap"],  # hip-hop hiyerarşi ile düştü
        "weights": [0.40, 0.25],
        "sources": {
            "hip-hop": ["deezer_artist"],  # güvenilir taban (slots'ta yok!)
            "drill": ["lastfm_artist"],
            "trap": ["lastfm_artist"],
        },
    }
    result = genre_runner._build_from_artist_base(profile)
    assert "hip-hop" in result["slots"]  # taban sources'tan bulundu
    assert "drill" in result["slots"]    # aile-uyumlu → eklendi
    assert "trap" in result["slots"]


# ─────────────────────────────────────────────────────────────────────
# _recheck_resolved_artists — otomatik geri kontrol (BÖLÜM 3, 2026-07-03)
# ─────────────────────────────────────────────────────────────────────
class _RecheckClient:
    """Geri kontrol update'lerini izler: hangi sanatçı için failed işareti temizlendi."""
    def __init__(self):
        self.cleared_artists: list[str] = []

    def table(self, name):
        client = self
        class _Q:
            def __init__(self):
                self._payload = None
                self._contains_val = None
            def update(self, payload):
                self._payload = payload
                return self
            @property
            def not_(self): return self
            def is_(self, *a, **k): return self
            def contains(self, col, val):
                self._contains_val = val
                return self
            def eq(self, *a, **k): return self
            def execute(self):
                # genre_lookup_failed_at = None temizleme + contains(artists, [X])
                if (self._payload and self._payload.get("genre_lookup_failed_at") is None
                        and self._contains_val):
                    client.cleared_artists.append(self._contains_val[0])
                return type("R", (), {"data": []})()
        return _Q()


def test_recheck_profili_dolu_sanatci_failed_temizler():
    """Bu turda profili DOLU çıkan sanatçının failed track işareti temizlenir."""
    client = _RecheckClient()
    artist_profiles = {
        "Ceza": {"slots": ["hip-hop"], "weights": [0.40]},   # DOLU
    }
    count = genre_runner._recheck_resolved_artists(client, artist_profiles)
    assert "Ceza" in client.cleared_artists
    assert count == 1


def test_recheck_bos_profil_sanatciya_dokunmaz():
    """Profili BOŞ (lookup_failed) sanatçının failed track'lerine DOKUNULMAZ —
    sonsuz döngü koruması."""
    client = _RecheckClient()
    artist_profiles = {
        "NişUltra": {"slots": [], "weights": []},  # BOŞ
    }
    count = genre_runner._recheck_resolved_artists(client, artist_profiles)
    assert client.cleared_artists == []
    assert count == 0


def test_recheck_karisik_sadece_dolulari_temizler():
    """Karışık: dolu sanatçılar temizlenir, boşlar atlanır."""
    client = _RecheckClient()
    artist_profiles = {
        "Ceza": {"slots": ["hip-hop"], "weights": [0.40]},   # DOLU
        "Boş1": {"slots": [], "weights": []},                # BOŞ
        "Şehinşah": {"slots": ["trap"], "weights": [0.40]},  # DOLU
    }
    count = genre_runner._recheck_resolved_artists(client, artist_profiles)
    assert set(client.cleared_artists) == {"Ceza", "Şehinşah"}
    assert count == 2


def test_artist_base_lastfm_only_trusted_profil_taban_olur():
    """Güven kuralı sonrası (2026-07-03): artist profiline YAZILMIŞ lastfm türü
    zaten TRUSTED'dır (SUSPECT olanlar _build_profile merge'inden çıkarılmıştı).
    Bu yüzden lastfm_artist artık geçerli taban sayılır — Ceza/Şehinşah gibi
    tek-tür TRUSTED sanatçıların track'leri failed kalmamalı."""
    profile = {
        "slots": ["trap"],
        "weights": [0.40],
        "sources": {
            "trap": ["lastfm_artist"],  # tek tür → güven kuralında TRUSTED'dı
        },
    }
    result = genre_runner._build_from_artist_base(profile)
    assert "trap" in result["slots"]  # lastfm-only TRUSTED profil taban olur


def test_artist_base_bos_profil_bos_doner():
    """Profil tamamen boşsa (hiç slot yok) yine boş döner — regresyon guard."""
    profile = {"slots": [], "weights": [], "sources": {}}
    result = genre_runner._build_from_artist_base(profile)
    assert result["slots"] == []


# ─────────────────────────────────────────────────────────────────────
# run_one_genre_batch — uçtan uca: genre_data + genres yazımı
# ─────────────────────────────────────────────────────────────────────
def test_batch_genre_data_ve_genres_yazilir(monkeypatch):
    monkeypatch.setattr(genre_runner, "get_deezer_track_scored_with_anchor",
                        lambda a, t, http: ([("Rap/Hip Hop", 0)], "Ezhel", 111))
    # Artist profili boş → boş slot doldurma etkisiz
    monkeypatch.setattr(genre_runner, "get_or_build_artist_profile",
                        lambda *a, **k: {"slots": [], "weights": []})

    track = {"id": "xyz", "title": "Geceler", "artists": ["Ezhel"]}
    client = _WriteClient(track_rows=[track])
    genre_runner.run_one_genre_batch(client, _settings())

    assert "xyz" in client.written
    payload = client.written["xyz"]
    assert payload["genres"] == ["hip-hop"]
    assert payload["genre_data"]["slots"] == ["hip-hop"]
    assert payload["genre_source"] is not None


def test_batch_bos_genre_failed_at(monkeypatch):
    monkeypatch.setattr(genre_runner, "get_deezer_track_scored_with_anchor", lambda a, t, http: ([], None, None))
    monkeypatch.setattr(genre_runner, "get_or_build_artist_profile",
                        lambda *a, **k: {"slots": [], "weights": []})

    track = {"id": "abc", "title": "Bilinmez", "artists": ["GizliSanatci"]}
    client = _WriteClient(track_rows=[track])
    genre_runner.run_one_genre_batch(client, _settings())

    assert "abc" in client.failed_ids
    assert "abc" not in client.written


def test_batch_geri_kontrol_profili_dolan_sanatci(monkeypatch):
    """Entegrasyon: batch akışında profili DOLU çıkan sanatçı için cron sonunda
    geri kontrol çalışır (artist_profiles scope'u sağlam + _recheck çağrılıyor)."""
    monkeypatch.setattr(genre_runner, "get_deezer_track_scored_with_anchor", lambda a, t, http: ([], None, None))
    monkeypatch.setattr(genre_runner, "get_or_build_artist_profile",
                        lambda *a, **k: {
                            "slots": ["trap"], "weights": [0.40],
                            "sources": {"trap": ["lastfm_artist"]},
                        })

    track = {"id": "s1", "title": "Ihtan", "artists": ["Şehinşah"]}
    client = _WriteClient(track_rows=[track])
    genre_runner.run_one_genre_batch(client, _settings())

    # Cron sonunda geri kontrol Şehinşah'ı temizledi (aynı sanatçının başka failed
    # track'leri sonraki turda dolacak)
    assert "Şehinşah" in client.recheck_cleared


def test_batch_bos_track_deezer_taban_ile_dolar(monkeypatch):
    # Track kaynakları boş. Artist profilinde Deezer-kaynaklı taban (hip-hop) var
    # → taban kurulur, Last.fm aile-uyumlu türler (drill, trap) eklenir.
    monkeypatch.setattr(genre_runner, "get_deezer_track_scored_with_anchor", lambda a, t, http: ([], None, None))
    monkeypatch.setattr(genre_runner, "get_or_build_artist_profile",
                        lambda *a, **k: {
                            "slots": ["hip-hop", "drill", "trap"],
                            "weights": [0.40, 0.25, 0.15],
                            "sources": {
                                "hip-hop": ["deezer_artist"],   # Deezer taban (isim doğrulanmış)
                                "drill": ["lastfm_artist"],
                                "trap": ["lastfm_artist"],
                            },
                        })

    track = {"id": "t1", "title": "Cinderella", "artists": ["Yung Ouzo"]}
    client = _WriteClient(track_rows=[track])
    genre_runner.run_one_genre_batch(client, _settings())

    assert "t1" in client.written
    assert "hip-hop" in client.written["t1"]["genres"]
    assert "drill" in client.written["t1"]["genres"]  # aile-uyumlu → eklendi
    assert client.written["t1"]["genre_source"] == "artist_profile"


def test_batch_bos_track_bos_profil_failed(monkeypatch):
    # manifest bug'ı (güncel koruma, 2026-07-03): çakışan lastfm tag'leri (death
    # metal + pop, akraba değil = SUSPECT) resolve_lastfm_trust tarafından
    # _build_profile merge'inden ÇIKARILDIĞI için artist profili BOŞ döner →
    # track lookup_failed. Yani koruma artık profil-oluşturma katmanında (yanlış
    # tür profile hiç ulaşmaz), _build_from_artist_base katmanında değil.
    # NOT: manifest 28 track'li (eşik ÜSTÜ) → small_artist DEĞİL → failed (pending değil).
    monkeypatch.setattr(genre_runner, "get_deezer_track_scored_with_anchor", lambda a, t, http: ([], None, None))
    monkeypatch.setattr(genre_runner, "get_db_artist_track_count", lambda *a, **k: 28)
    monkeypatch.setattr(genre_runner, "get_or_build_artist_profile",
                        lambda *a, **k: {
                            "slots": [],  # SUSPECT lastfm elendi → profil boş
                            "weights": [],
                            "sources": {},
                        })

    track = {"id": "m1", "title": "Hileli", "artists": ["manifest"]}
    client = _WriteClient(track_rows=[track])
    genre_runner.run_one_genre_batch(client, _settings())

    assert "m1" in client.failed_ids       # profil boş (≥5 track) → failed
    assert "m1" not in client.pending_ids  # pending DEĞİL (eşik üstü sanatçı)
    assert "m1" not in client.written      # yanlış tür YAZILMADI


def test_reactivate_small_artist_pending_rpc_cagrilir(monkeypatch):
    """Cron başında reactivate_small_artist_pending RPC'si çağrılır — track sayısı
    ≥5'e ulaşmış sanatçıların pending'i temizlenir (olay-tabanlı, 2026-07-05)."""
    calls = []

    class _RpcClient:
        def rpc(self, name, params):
            calls.append(name)
            class _Q:
                def execute(self_):
                    return type("R", (), {"data": 0})()
            return _Q()
        def table(self, *a, **k):
            # boş batch → cron erken çıkar, ama RPC zaten çağrılmış olmalı
            return _Query([])

    genre_runner.run_one_genre_batch(_RpcClient(), _settings())
    assert "reactivate_small_artist_pending" in calls


def test_batch_az_trackli_sanatci_bos_track_pending(monkeypatch):
    """Az-track'li (<5) sanatçının kendi türü de bulunamayan track'i → 'pending'
    (lookup_failed DEĞİL). Mahmut Tuncer/Yıldız Tilbe koruması (2026-07-05).
    Profil hiç kurulmaz (dış kaynak çağrılmaz), track sonraki turlarda cron'dan
    genre_pending_reason ile atlanır; sanatçı ≥5 track olunca yeniden denenir."""
    monkeypatch.setattr(genre_runner, "get_deezer_track_scored_with_anchor", lambda a, t, http: ([], None, None))
    # Sanatçının DB'de yalnızca 1 track'i var (eşik altı)
    monkeypatch.setattr(genre_runner, "get_db_artist_track_count", lambda *a, **k: 1)

    def _boom(*a, **k):
        raise AssertionError("Az-track'li sanatçıda artist profili KURULMAMALI")
    monkeypatch.setattr(genre_runner, "get_or_build_artist_profile", _boom)

    track = {"id": "mt1", "title": "Sabunu Koydum Leğene", "artists": ["Mahmut Tuncer"]}
    client = _WriteClient(track_rows=[track])
    genre_runner.run_one_genre_batch(client, _settings())

    assert "mt1" in client.pending_ids    # pending → ileride yeniden denenir
    assert "mt1" not in client.failed_ids # KALICI kilit yazılmadı
    assert "mt1" not in client.written    # tür yazılmadı


def test_batch_bos_track_lastfm_trusted_profil_dolar(monkeypatch):
    # Yeni davranış (2026-07-03): track boş + sanatçı profili lastfm-only ama
    # TRUSTED (Ceza/Şehinşah gibi tek tür) → track artist_profile'dan dolar,
    # failed KALMAZ. Dalga 1'de yüzlerce track'i kurtaran düzeltme.
    monkeypatch.setattr(genre_runner, "get_deezer_track_scored_with_anchor", lambda a, t, http: ([], None, None))
    monkeypatch.setattr(genre_runner, "get_or_build_artist_profile",
                        lambda *a, **k: {
                            "slots": ["trap"],
                            "weights": [0.40],
                            "sources": {"trap": ["lastfm_artist"]},  # TRUSTED (tek tür)
                        })

    track = {"id": "s1", "title": "Ihtan", "artists": ["Şehinşah"]}
    client = _WriteClient(track_rows=[track])
    genre_runner.run_one_genre_batch(client, _settings())

    assert "s1" not in client.failed_ids            # failed KALMADI
    assert "trap" in client.written["s1"]["genres"] # artist profilinden doldu
    assert client.written["s1"]["genre_source"] == "artist_profile"


# ─────────────────────────────────────────────────────────────────────
# genre_pending temizleme (korunan mantık)
# ─────────────────────────────────────────────────────────────────────
class _PendingClient:
    def __init__(self, genres_null_rows):
        self._null_rows = genres_null_rows
        self.cleared = False
    def table(self, name):
        client = self
        class _Q:
            def __init__(self):
                self._is_export_update = False
            def select(self, *a, **k): return self
            def is_(self, *a, **k): return self
            def limit(self, *a, **k): return self
            def update(self, payload):
                if name == "export_jobs" and payload.get("genre_pending") is False:
                    self._is_export_update = True
                return self
            def eq(self, *a, **k): return self
            def execute(self):
                if self._is_export_update:
                    client.cleared = True
                return type("R", (), {"data": client._null_rows if name == "tracks" else []})()
        return _Q()


def test_genre_pending_temizlenir_eksik_yokken():
    client = _PendingClient(genres_null_rows=[])
    genre_runner._clear_genre_pending_if_done(client)
    assert client.cleared is True


def test_genre_pending_temizlenmez_eksik_varken():
    client = _PendingClient(genres_null_rows=[{"id": "x"}])
    genre_runner._clear_genre_pending_if_done(client)
    assert client.cleared is False


def test_build_track_genre_data_two_stage_and_anchor(monkeypatch):
    """Ham başlık boşsa temiz başlıkla tekrar dener; çapa döner."""
    from app.pipeline import genre_runner

    calls = []

    def fake_deezer_anchor(artist, title, http):
        calls.append(title)
        if "feat" in title.lower():
            return [], None, None  # ham başlık (feat.'li) boş
        return [("Rap/Hip Hop", 0)], "Murda", 222  # temiz başlık bulur

    monkeypatch.setattr(genre_runner, "get_deezer_track_scored_with_anchor", fake_deezer_anchor)

    data, anchor, _, _ = genre_runner.build_track_genre_data(
        "Murda", "Gece Gündüz (feat. MERO)", lastfm_key="", http=None
    )

    assert calls == ["Gece Gündüz (feat. MERO)", "Gece Gündüz"]  # iki aşama
    assert anchor == "Murda"
    assert data["slots"]


def test_build_track_genre_data_returns_anchor_id(monkeypatch):
    """build_track_genre_data track çapasının artist.id'sini de döndürür (4. eleman
    değil — 3. eleman anchor_id, 4. rate_limited). Deezer isim araması yanlış
    sanatçı bulduğu için artist DNA bu id'den çözülür (2026-07-04)."""
    from app.pipeline import genre_runner

    monkeypatch.setattr(
        genre_runner, "get_deezer_track_scored_with_anchor",
        lambda a, t, http: ([("Rap/Hip Hop", 0)], "Motive", 72196),
    )

    data, anchor, anchor_id, rl = genre_runner.build_track_genre_data(
        "Motive", "Makaveli", lastfm_key="", http=None
    )
    assert anchor == "Motive"
    assert anchor_id == 72196
    assert rl is None
    assert data["slots"]


def test_build_track_genre_data_raw_hit_no_second_stage(monkeypatch):
    """Ham başlık bulunursa temiz başlıkla tekrar DENENMEZ."""
    from app.pipeline import genre_runner

    calls = []

    def fake_deezer_anchor(artist, title, http):
        calls.append(title)
        return [("R&B", 0)], "Ty Dolla $ign", 333

    monkeypatch.setattr(genre_runner, "get_deezer_track_scored_with_anchor", fake_deezer_anchor)

    data, anchor, _, _ = genre_runner.build_track_genre_data(
        "Ty Dolla $ign", "Spicy (feat. Post Malone)", lastfm_key="", http=None
    )

    assert calls == ["Spicy (feat. Post Malone)"]  # tek aşama
    assert anchor == "Ty Dolla $ign"


def test_batch_passes_verified_anchor_to_profile(monkeypatch):
    """Çapa is_artist_match geçerse (DB adıyla ~aynı) profile query_name=anchor aktarılır.

    Senaryo: DB adı 'manifest' (küçük harf), Deezer çapası 'Manifest' (büyük M) —
    difflib eşiği geçer, çapa güvenilir sorgu adı olur. Cache anahtarı DB adı kalır.
    """
    from app.pipeline import genre_runner

    # Track tamamen boş dönsün ki artist profili devreye girsin; çapa 'Manifest', id 900
    monkeypatch.setattr(
        genre_runner, "build_track_genre_data",
        lambda artist, title, *, lastfm_key, http: (
            {"slots": [], "weights": [], "raw_scores": {}, "sources": {}}, "Manifest", 900, None
        ),
    )

    captured = {}

    def fake_profile(artist, client, http, lastfm_key, query_name=None, query_id=None):
        captured["artist"] = artist
        captured["query_name"] = query_name
        captured["query_id"] = query_id
        return {"slots": ["pop"], "weights": [1.0], "raw_scores": {},
                "sources": {"pop": ["deezer_artist"]}}

    monkeypatch.setattr(genre_runner, "get_or_build_artist_profile", fake_profile)
    monkeypatch.setattr(genre_runner.cooldown, "is_blocked", lambda c, p: (False, None))

    rows = [{"id": "t1", "title": "Yaşanacaksa", "artists": ["manifest"]}]
    client = _WriteClient(rows)
    settings = type("S", (), {"lastfm_api_key": "key"})()

    genre_runner.run_one_genre_batch(client, settings, batch_limit=10)

    # cache anahtarı DB adı (manifest), sorgu doğrulanmış çapa (Manifest) + id (900)
    assert captured["artist"] == "manifest"
    assert captured["query_name"] == "Manifest"
    assert captured["query_id"] == 900  # doğrulanmış çapa id'si de taşınır


def test_batch_profile_uses_any_track_anchor_id_not_just_first(monkeypatch):
    """Profil, aynı sanatçının HERHANGİ track'inden gelen ilk geçerli anchor_id'yi
    kullanır — ilk track'in şansına bırakmaz (2026-07-04, Motive senaryosu).

    Senaryo: Motive'in t1 track'i Deezer'da YOK (anchor_id=None), t2 track'i VAR
    (anchor_id=72196). Profil t2'nin id'sinden kurulmalı, t1'in None'ından değil.
    Aksi halde profil isim yoluna düşüp yanlış tür alır (elektronik)."""
    from app.pipeline import genre_runner

    def fake_build(artist, title, *, lastfm_key, http):
        empty = {"slots": [], "weights": [], "raw_scores": {}, "sources": {}}
        if title == "feat-track":
            return empty, None, None, None          # Deezer'da yok → id yok
        return empty, "Motive", 72196, None          # bulundu → doğru id

    monkeypatch.setattr(genre_runner, "build_track_genre_data", fake_build)

    captured = {}

    def fake_profile(artist, client, http, lastfm_key, query_name=None, query_id=None):
        captured["query_id"] = query_id
        captured["query_name"] = query_name
        return {"slots": ["hip-hop"], "weights": [1.0], "raw_scores": {},
                "sources": {"hip-hop": ["deezer_artist"]}}

    monkeypatch.setattr(genre_runner, "get_or_build_artist_profile", fake_profile)
    monkeypatch.setattr(genre_runner.cooldown, "is_blocked", lambda c, p: (False, None))

    # t1 önce (id yok), t2 sonra (id var) — sıra önemli: eski kod t1'in None'ını alırdı
    rows = [
        {"id": "t1", "title": "feat-track", "artists": ["Motive"]},
        {"id": "t2", "title": "Makaveli", "artists": ["Motive"]},
    ]
    genre_runner.run_one_genre_batch(_WriteClient(rows),
                                     type("S", (), {"lastfm_api_key": "key"})(),
                                     batch_limit=10)

    assert captured["query_id"] == 72196      # t2'nin geçerli id'si kullanıldı
    assert captured["query_name"] == "Motive"


def test_batch_skips_anchor_when_name_mismatch(monkeypatch):
    """Çapa is_artist_match geçmezse query_name aktarılmaz (None)."""
    from app.pipeline import genre_runner

    monkeypatch.setattr(
        genre_runner, "build_track_genre_data",
        lambda artist, title, *, lastfm_key, http: (
            {"slots": [], "weights": [], "raw_scores": {}, "sources": {}},
            "Completely Different Artist",
            555,
            None,
        ),
    )

    captured = {}
    monkeypatch.setattr(
        genre_runner, "get_or_build_artist_profile",
        lambda artist, client, http, lastfm_key, query_name=None, query_id=None: (
            captured.update(query_name=query_name, query_id=query_id)
            or {"slots": ["pop"], "weights": [1.0], "raw_scores": {},
                "sources": {"pop": ["deezer_artist"]}}
        ),
    )
    monkeypatch.setattr(genre_runner.cooldown, "is_blocked", lambda c, p: (False, None))

    rows = [{"id": "t1", "title": "X", "artists": ["manifest"]}]
    genre_runner.run_one_genre_batch(_WriteClient(rows),
                                     type("S", (), {"lastfm_api_key": "key"})(),
                                     batch_limit=10)

    # isim eşleşmedi → hem çapa adı hem id kullanılmaz (yanlış id taşınmaz)
    assert captured["query_name"] is None
    assert captured["query_id"] is None


def test_batch_429_triggers_set_cooldown_and_no_failed(monkeypatch):
    """Track 429 alırsa set_cooldown çağrılır, track lookup_failed YAZILMAZ."""
    from app.pipeline import genre_runner
    from app.services.genre_errors import RateLimitError

    def raise_429(artist, title, http):
        raise RateLimitError("deezer", 45.0)

    monkeypatch.setattr(genre_runner, "get_deezer_track_scored_with_anchor", raise_429)
    monkeypatch.setattr(genre_runner.cooldown, "is_blocked", lambda c, p: (False, None))

    cooldowns = []
    monkeypatch.setattr(genre_runner.cooldown, "set_cooldown",
                        lambda client, provider, retry_after, reason: cooldowns.append((provider, retry_after)))

    rows = [{"id": "t1", "title": "Song", "artists": ["Artist"]}]
    client = _WriteClient(rows)
    genre_runner.run_one_genre_batch(client, _settings())

    assert ("deezer", 45.0) in cooldowns   # cooldown beslendi
    assert "t1" not in client.failed_ids   # 429 → lookup_failed DEĞİL
    assert "t1" not in client.written      # yazım da yok (retry edilecek)


def test_batch_artist_profile_429_no_failed_write(monkeypatch):
    """Artist profili 429 alırsa (get_deezer_artist_genres_by_id → RateLimitError),
    track lookup_failed YAZILMAZ + cooldown beslenir (2026-07-04 canlı bulgu:
    Motive profili paralel yükte Deezer 429 yiyip elektroniğe düşüyordu).

    Senaryo: track Deezer'da bulunamaz (boş) → artist profiline düşer → profil
    kurulurken by_id 429 fırlatır. Track sonraki tura kalmalı, yanlış tür yazılmamalı."""
    from app.pipeline import genre_runner
    from app.services.genre_errors import RateLimitError

    # Track boş dön (profil devreye girsin), çapa id'li
    monkeypatch.setattr(
        genre_runner, "build_track_genre_data",
        lambda artist, title, *, lastfm_key, http: (
            {"slots": [], "weights": [], "raw_scores": {}, "sources": {}}, "Motive", 72196, None
        ),
    )
    # Profil kurulumu 429 fırlatır (by_id rate-limit)
    def raise_429(*a, **k):
        raise RateLimitError("deezer", 30.0)
    monkeypatch.setattr(genre_runner, "get_or_build_artist_profile", raise_429)
    monkeypatch.setattr(genre_runner.cooldown, "is_blocked", lambda c, p: (False, None))

    cooldowns = []
    monkeypatch.setattr(genre_runner.cooldown, "set_cooldown",
                        lambda client, provider, retry_after, reason: cooldowns.append((provider, retry_after)))

    rows = [{"id": "t1", "title": "ROMANTİK", "artists": ["Motive"]}]
    client = _WriteClient(rows)
    genre_runner.run_one_genre_batch(client, _settings())

    assert ("deezer", 30.0) in cooldowns   # cooldown beslendi
    assert "t1" not in client.failed_ids   # lookup_failed YAZILMADI
    assert "t1" not in client.written      # yanlış tür de yazılmadı


def test_build_track_genre_data_returns_rate_limit_signal(monkeypatch):
    """429'da build_track_genre_data (empty, None, (provider, retry_after)) döner."""
    from app.pipeline import genre_runner
    from app.services.genre_errors import RateLimitError

    def raise_429(artist, title, http):
        raise RateLimitError("deezer", 45.0)

    monkeypatch.setattr(genre_runner, "get_deezer_track_scored_with_anchor", raise_429)

    data, anchor, _, rl = genre_runner.build_track_genre_data("A", "B", lastfm_key="k", http=None)
    assert data["slots"] == []
    assert rl == ("deezer", 45.0)

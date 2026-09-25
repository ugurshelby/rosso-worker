"""playlist_refresh_runner — snapshot_id diff ile playlist tazeleme testleri."""
import pytest

from app.pipeline import playlist_refresh_runner as runner
from app.services.api_gate import GateDecision


@pytest.fixture(autouse=True)
def _gecit_acik(monkeypatch):
    """Merkezî geçit varsayılan olarak AÇIK (plan 08, 2026-08-05).

    Runner artık her turun başında `api_gate.check` çağırıyor; fake client
    `budget_check_and_consume` RPC'sini bilmediği için gerçek geçit "kapalı"
    döner ve TÜM testler anlamsızca `skipped` alırdı. Geçidin kendi davranışı
    ayrıca test edilir (aşağıda), burada açık varsayılır.
    """
    monkeypatch.setattr(
        runner.api_gate, "check",
        lambda c, s, **k: GateDecision(allowed=True, reason="ok",
                                       remaining=299, used=1, budget=300),
    )
    monkeypatch.setattr(runner.api_gate, "refund", lambda c, s, n=1: None)


class _WriteClient:
    def __init__(self, connections, existing_playlists):
        self._connections = connections
        self._existing_playlists = existing_playlists
        self.playlists_upserted: list[dict] = []
        self.playlist_tracks_deleted_for: list[str] = []
        self.playlist_tracks_inserted: list[dict] = []
        self.tracks_inserted: list[dict] = []
        self.playlists_deleted_ids: list[str] = []  # SBA-7: reconcile ile silinen platform_id'ler
        self.connection_updates: list[dict] = []  # spotify_user_id geri yazımı

    def table(self, name):
        client = self
        class _Q:
            def __init__(self):
                self._filters = {}
                self._is_delete = False
                self._is_insert = False
                self._in_values = None
            def select(self, *a, **k): return self
            def eq(self, col, val):
                self._filters[col] = val
                if self._is_delete and col == "playlist_id":
                    client.playlist_tracks_deleted_for.append(val)
                return self
            def in_(self, col, values):
                self._in_values = (col, list(values))
                if self._is_delete and col == "platform_id":
                    client.playlists_deleted_ids.extend(values)
                return self
            def limit(self, *a, **k): return self
            def ilike(self, *a, **k): return self
            def is_(self, col, val):
                self._filters[f"is_{col}"] = val
                return self
            def update(self, payload):
                self._payload = payload
                client.connection_updates.append(payload)
                return self
            def delete(self):
                self._is_delete = True
                return self
            def upsert(self, payload, **k):
                self._payload = payload
                return self
            def insert(self, payload):
                self._payload = payload
                self._is_insert = True
                return self
            def execute(self):
                if name == "platform_connections":
                    return type("R", (), {"data": client._connections})()
                if name == "playlists" and self._is_delete:
                    return type("R", (), {"data": []})()
                if name == "playlists" and hasattr(self, "_payload"):
                    client.playlists_upserted.append(self._payload)
                    return type("R", (), {"data": [{**self._payload, "id": "pl-db-1"}]})()
                if name == "playlists":
                    return type("R", (), {"data": client._existing_playlists})()
                if name == "playlist_tracks" and self._is_delete:
                    return type("R", (), {"data": []})()
                if name == "playlist_tracks" and hasattr(self, "_payload") and getattr(self, "_is_insert", False):
                    client.playlist_tracks_inserted.append(self._payload)
                    return type("R", (), {"data": []})()
                if name == "tracks" and getattr(self, "_is_insert", False):
                    new_id = f"new-track-{len(client.tracks_inserted)}"
                    client.tracks_inserted.append(self._payload)
                    return type("R", (), {"data": [{**self._payload, "id": new_id}]})()
                return type("R", (), {"data": []})()
        return _Q()


def test_snapshot_id_unchanged_skips_item_fetch(monkeypatch):
    """snapshot_id (fetch_user_playlists yanıtından) DB'dekiyle aynıysa
    fetch_playlist_items ÇAĞRILMAZ — ayrı bir snapshot_id GET'i de YAPILMAZ."""
    monkeypatch.setattr(runner, "get_valid_spotify_token", lambda c, u, k, h: "tok")
    monkeypatch.setattr(runner, "fetch_user_playlists", lambda tok, h: [
        {"spotify_id": "pl1", "name": "P1", "snapshot_id": "same", "track_count": 5},
    ])

    called = {"items": False}
    def fake_items(tok, pid, h):
        called["items"] = True
        return []
    monkeypatch.setattr(runner, "fetch_playlist_items", fake_items)

    client = _WriteClient(
        connections=[{"user_id": "u1", "access_token": "enc"}],
        existing_playlists=[{"id": "pl-db-1", "platform_id": "pl1", "snapshot_id": "same"}],
    )
    result = runner.run_one_playlist_refresh(client, http=None, crypto_key="key")

    assert called["items"] is False
    assert result["outcome"] == "success"


def test_snapshot_id_changed_fetches_items_and_reinserts(monkeypatch):
    """snapshot_id değiştiyse item'lar çekilir; playlist_tracks önce silinir
    sonra sırayla yeniden eklenir (UNIQUE(playlist_id, position) constraint'i
    upsert ile çakışır — Fable 5 keşif bulgusu, 2026-07-02)."""
    monkeypatch.setattr(runner, "get_valid_spotify_token", lambda c, u, k, h: "tok")
    monkeypatch.setattr(runner, "fetch_user_playlists", lambda tok, h: [
        {"spotify_id": "pl1", "name": "P1", "snapshot_id": "new", "track_count": 1},
    ])
    monkeypatch.setattr(runner, "fetch_playlist_items", lambda tok, pid, h: [
        {"spotify_id": "t1", "isrc": None, "title": "Song", "artists": ["A"],
         "position": 0, "added_at": "2026-01-01T00:00:00Z"},
    ])

    client = _WriteClient(
        connections=[{"user_id": "u1", "access_token": "enc"}],
        existing_playlists=[{"id": "pl-db-1", "platform_id": "pl1", "snapshot_id": "old"}],
    )
    result = runner.run_one_playlist_refresh(client, http=None, crypto_key="key")

    assert len(client.playlists_upserted) == 1
    assert client.playlists_upserted[0]["snapshot_id"] == "new"
    assert "pl-db-1" in client.playlist_tracks_deleted_for  # önce tam silme
    assert len(client.playlist_tracks_inserted) == 1        # sonra yeniden ekleme
    assert client.playlist_tracks_inserted[0]["position"] == 0
    # UI'da "son senkron" göstergesi için synced_at doldurulmalı (2026-07-03 keşfi:
    # playlists.synced_at kolonu var ama hiç yazılmıyordu)
    assert client.playlists_upserted[0]["synced_at"] is not None


def test_only_own_playlists_processed(monkeypatch):
    """B6: kullanıcının SAHİBİ olmadığı (takip ettiği) playlist item fetch'e gitmez.
    owner_id != me_id olan liste atlanır."""
    monkeypatch.setattr(runner, "get_valid_spotify_token", lambda c, u, k, h: "tok")
    monkeypatch.setattr(runner, "_fetch_me_id", lambda tok, h, **kw: "me-123")
    monkeypatch.setattr(runner, "fetch_user_playlists", lambda tok, h: [
        {"spotify_id": "own", "name": "Benim", "snapshot_id": "s1", "track_count": 1, "owner_id": "me-123"},
        {"spotify_id": "followed", "name": "Başkasının", "snapshot_id": "s2", "track_count": 9, "owner_id": "other-999"},
    ])
    fetched = []
    monkeypatch.setattr(runner, "fetch_playlist_items", lambda tok, pid, h: fetched.append(pid) or [])

    client = _WriteClient(
        connections=[{"user_id": "u1", "access_token": "enc"}],
        existing_playlists=[],
    )
    runner.run_one_playlist_refresh(client, http=object(), crypto_key="key")

    assert fetched == ["own"]  # yalnız kendi listesi işlendi, takip edilen atlandı


def test_playlist_403_skips_that_playlist_not_whole_cron(monkeypatch):
    """B1: bir playlist item fetch'i 403 verirse o liste atlanır, cron ÇÖKMEZ,
    sonraki playlist işlenmeye devam eder."""
    monkeypatch.setattr(runner, "get_valid_spotify_token", lambda c, u, k, h: "tok")
    monkeypatch.setattr(runner, "_fetch_me_id", lambda tok, h, **kw: None)  # filtre yok
    monkeypatch.setattr(runner, "fetch_user_playlists", lambda tok, h: [
        {"spotify_id": "bad", "name": "Kötü", "snapshot_id": "s1", "track_count": 1, "owner_id": None},
        {"spotify_id": "good", "name": "İyi", "snapshot_id": "s2", "track_count": 1, "owner_id": None},
    ])

    class _Resp:
        status_code = 403
    class _HttpErr(Exception):
        response = _Resp()

    def items(tok, pid, h):
        if pid == "bad":
            raise _HttpErr()
        return [{"spotify_id": "t1", "isrc": None, "title": "Song", "artists": ["A"],
                 "position": 0, "added_at": "2026-01-01T00:00:00Z"}]
    monkeypatch.setattr(runner, "fetch_playlist_items", items)

    client = _WriteClient(
        connections=[{"user_id": "u1", "access_token": "enc"}],
        existing_playlists=[],
    )
    result = runner.run_one_playlist_refresh(client, http=object(), crypto_key="key")

    # "good" işlendi (cron ölmedi), "bad" atlandı — ama S2: sonuç dürüst 'partial',
    # atlanan liste sayısı görünür (eski 'success' beklentisi bug'dı).
    assert result["outcome"] == "partial"
    assert result["skipped_playlists"] == 1
    assert len(client.playlist_tracks_inserted) == 1


def test_playlist_5xx_still_raises(monkeypatch):
    """B1 sınırı: 403/404 dışındaki hata (5xx) YUTULMAZ — cron kaydına yansısın."""
    monkeypatch.setattr(runner, "get_valid_spotify_token", lambda c, u, k, h: "tok")
    monkeypatch.setattr(runner, "_fetch_me_id", lambda tok, h, **kw: None)
    monkeypatch.setattr(runner, "fetch_user_playlists", lambda tok, h: [
        {"spotify_id": "pl", "name": "P", "snapshot_id": "s1", "track_count": 1, "owner_id": None},
    ])

    class _Resp:
        status_code = 503
    class _HttpErr(Exception):
        response = _Resp()
    monkeypatch.setattr(runner, "fetch_playlist_items", lambda tok, pid, h: (_ for _ in ()).throw(_HttpErr()))

    client = _WriteClient(connections=[{"user_id": "u1", "access_token": "enc"}], existing_playlists=[])
    import pytest
    with pytest.raises(Exception):
        runner.run_one_playlist_refresh(client, http=object(), crypto_key="key")


def test_track_insert_conflict_rereads(monkeypatch):
    """B4: playlist track INSERT çakışırsa None dönmez, tekrar okunup id döndürülür."""
    class _RaceRepo:
        def __init__(self): self.calls = 0
        def find_by_spotify_id(self, sid):
            self.calls += 1
            return {"id": "existing"} if self.calls >= 2 else None

    repo = _RaceRepo()
    class _ConflictClient(_WriteClient):
        def table(self, name):
            if name == "tracks":
                class _T:
                    def insert(self, p): return self
                    def execute(self): raise RuntimeError("duplicate key")
                    def select(self, *a, **k): return self
                    def eq(self, *a, **k): return self
                    def limit(self, *a, **k): return self
                    def ilike(self, *a, **k): return self
                return _T()
            return super().table(name)

    client = _ConflictClient(connections=[], existing_playlists=[])
    item = {"spotify_id": "sp1", "isrc": None, "title": "T", "artists": ["A"], "position": 0}
    result = runner._resolve_or_create_track(client, repo, item)
    assert result == "existing"  # kayıp yok, tekrar okundu


def test_resolve_or_create_track_skips_empty_garbage():
    """Boş title/artists/spotify_id olan item için track INSERT edilmez, None
    döner (P0 çöp bug'ı guard'ı, 2026-07-03)."""
    class _Repo:
        def find_by_spotify_id(self, sid):
            return None
    client = _WriteClient(connections=[], existing_playlists=[])

    garbage = {"spotify_id": None, "isrc": None, "title": "", "artists": [], "position": 0}
    result = runner._resolve_or_create_track(client, _Repo(), garbage)

    assert result is None
    assert client.tracks_inserted == []  # hiç insert yok


def test_garbage_items_not_inserted_into_playlist_tracks(monkeypatch):
    """fetch_playlist_items çöp item döndürürse (boş item), playlist_tracks'e
    hiçbir satır yazılmaz."""
    monkeypatch.setattr(runner, "get_valid_spotify_token", lambda c, u, k, h: "tok")
    monkeypatch.setattr(runner, "fetch_user_playlists", lambda tok, h: [
        {"spotify_id": "pl1", "name": "P1", "snapshot_id": "new", "track_count": 2},
    ])
    monkeypatch.setattr(runner, "fetch_playlist_items", lambda tok, pid, h: [
        {"spotify_id": None, "isrc": None, "title": "", "artists": [], "position": 0,
         "added_at": None},
        {"spotify_id": "t1", "isrc": None, "title": "Real", "artists": ["A"],
         "position": 1, "added_at": "2026-01-01T00:00:00Z"},
    ])

    client = _WriteClient(
        connections=[{"user_id": "u1", "access_token": "enc"}],
        existing_playlists=[{"id": "pl-db-1", "platform_id": "pl1", "snapshot_id": "old"}],
    )
    runner.run_one_playlist_refresh(client, http=None, crypto_key="key")

    # Sadece gerçek track (t1) yazılmalı, çöp değil
    assert len(client.playlist_tracks_inserted) == 1
    assert len(client.tracks_inserted) == 1


def test_kapak_ve_aciklama_upsert_edilir(monkeypatch):
    """FAZ SBA-3 (2026-07-21): ayrıştırıcı cover_url/description'ı aynı Spotify
    yanıtından alıyor; upsert bunları DB'ye YAZMALI. Önceki hâlde 105/105
    playlist'te iki alan da NULL'dı — üç ekran bunları okuduğu hâlde."""
    monkeypatch.setattr(runner, "get_valid_spotify_token", lambda c, u, k, h: "tok")
    monkeypatch.setattr(runner, "fetch_user_playlists", lambda tok, h: [
        {"spotify_id": "pl1", "name": "P1", "snapshot_id": "new", "track_count": 1,
         "cover_url": "https://i/buyuk.jpg", "description": "Gece sürüşü"},
    ])
    monkeypatch.setattr(runner, "fetch_playlist_items", lambda tok, pid, h: [])

    client = _WriteClient(
        connections=[{"user_id": "u1", "access_token": "enc"}],
        existing_playlists=[{"id": "pl-db-1", "platform_id": "pl1", "snapshot_id": "old"}],
    )
    runner.run_one_playlist_refresh(client, http=None, crypto_key="key")

    assert client.playlists_upserted[0]["cover_url"] == "https://i/buyuk.jpg"
    assert client.playlists_upserted[0]["description"] == "Gece sürüşü"


def test_kapak_alanlari_eksikse_upsert_kirilmaz(monkeypatch):
    """Eski çağrı yolları bu anahtarları göndermeyebilir — .get() ile okunuyor,
    KeyError ile cron kırılmamalı, alanlar NULL kalmalı."""
    monkeypatch.setattr(runner, "get_valid_spotify_token", lambda c, u, k, h: "tok")
    monkeypatch.setattr(runner, "fetch_user_playlists", lambda tok, h: [
        {"spotify_id": "pl1", "name": "P1", "snapshot_id": "new", "track_count": 1},
    ])
    monkeypatch.setattr(runner, "fetch_playlist_items", lambda tok, pid, h: [])

    client = _WriteClient(
        connections=[{"user_id": "u1", "access_token": "enc"}],
        existing_playlists=[{"id": "pl-db-1", "platform_id": "pl1", "snapshot_id": "old"}],
    )
    runner.run_one_playlist_refresh(client, http=None, crypto_key="key")

    assert client.playlists_upserted[0]["cover_url"] is None
    assert client.playlists_upserted[0]["description"] is None


def test_snapshot_ayniyken_eksik_kapak_tamamlanir(monkeypatch):
    """🔴 SBA-3 TUZAĞI: snapshot değişmemişse erken `continue` upsert'i de
    atlıyordu. Kapak yazımını eklemek tek başına YETMEZDİ — mevcut 105
    playlist'in snapshot'ı değişmediği için alanlar sonsuza dek NULL kalırdı.

    Beklenen: item fetch YAPILMAZ (pahalı olan o), ama eksik metadata ucuz
    upsert'le tamamlanır. Ek API isteği yok — veri zaten listede geldi."""
    monkeypatch.setattr(runner, "get_valid_spotify_token", lambda c, u, k, h: "tok")
    monkeypatch.setattr(runner, "fetch_user_playlists", lambda tok, h: [
        {"spotify_id": "pl1", "name": "P1", "snapshot_id": "ayni", "track_count": 1,
         "cover_url": "https://i/kapak.jpg", "description": "Açıklama"},
    ])

    def _patlasin(*a, **k):
        raise AssertionError("snapshot aynıyken item fetch YAPILMAMALI")
    monkeypatch.setattr(runner, "fetch_playlist_items", _patlasin)

    client = _WriteClient(
        connections=[{"user_id": "u1", "access_token": "enc"}],
        existing_playlists=[{"id": "pl-db-1", "platform_id": "pl1",
                             "snapshot_id": "ayni", "cover_url": None,
                             "description": None}],
    )
    runner.run_one_playlist_refresh(client, http=None, crypto_key="key")

    assert len(client.playlists_upserted) == 1
    assert client.playlists_upserted[0]["cover_url"] == "https://i/kapak.jpg"
    assert client.playlists_upserted[0]["description"] == "Açıklama"
    # synced_at yazılmamalı: içerik senkronu olmadı, UI'a yalan söylenmemeli.
    assert "synced_at" not in client.playlists_upserted[0]


def test_snapshot_ayni_ve_kapak_zaten_varsa_hicbir_yazim_olmaz(monkeypatch):
    """Gereksiz yazım yok — kapak zaten doluysa DB'ye dokunulmaz."""
    monkeypatch.setattr(runner, "get_valid_spotify_token", lambda c, u, k, h: "tok")
    monkeypatch.setattr(runner, "fetch_user_playlists", lambda tok, h: [
        {"spotify_id": "pl1", "name": "P1", "snapshot_id": "ayni", "track_count": 1,
         "cover_url": "https://i/kapak.jpg", "description": "Açıklama"},
    ])
    monkeypatch.setattr(runner, "fetch_playlist_items", lambda *a, **k: [])

    client = _WriteClient(
        connections=[{"user_id": "u1", "access_token": "enc"}],
        existing_playlists=[{"id": "pl-db-1", "platform_id": "pl1",
                             "snapshot_id": "ayni",
                             "cover_url": "https://i/kapak.jpg",
                             "description": "Açıklama"}],
    )
    runner.run_one_playlist_refresh(client, http=None, crypto_key="key")

    assert client.playlists_upserted == []


# ── SBA-7: Spotify'da silinen playlist DB'den temizlenir (reconcile) ────────

def test_spotifyda_silinen_playlist_db_den_temizlenir(monkeypatch):
    """🔴 SBA-7 (2026-07-26): Spotify'da SİLİNEN playlist DB'de hayalet kalıyordu.
    Runner yalnız uzak listeler üzerinde döner; silineni hiç görmediği için DB
    satırı sonsuza dek durur ve Rosso sayfasında görünmeye devam eder.

    Beklenen: bu tur Spotify'dan dönen platform_id setinde OLMAYAN DB satırları
    silinir. Burada DB'de pl1+pl2 var, Spotify yalnız pl1 döndürüyor → pl2 silinir."""
    monkeypatch.setattr(runner, "get_valid_spotify_token", lambda c, u, k, h: "tok")
    monkeypatch.setattr(runner, "_fetch_me_id", lambda tok, h, **kw: "me-123")
    monkeypatch.setattr(runner, "fetch_user_playlists", lambda tok, h: [
        {"spotify_id": "pl1", "name": "Kalan", "snapshot_id": "ayni",
         "track_count": 1, "owner_id": "me-123"},
    ])
    monkeypatch.setattr(runner, "fetch_playlist_items", lambda *a, **k: [])

    client = _WriteClient(
        connections=[{"user_id": "u1", "access_token": "enc"}],
        existing_playlists=[
            {"id": "pl-db-1", "platform_id": "pl1", "snapshot_id": "ayni"},
            {"id": "pl-db-2", "platform_id": "pl2", "snapshot_id": "eski"},  # Spotify'da yok
        ],
    )
    result = runner.run_one_playlist_refresh(client, http=object(), crypto_key="key")

    assert client.playlists_deleted_ids == ["pl2"]  # yalnız hayalet silindi
    assert "pl1" not in client.playlists_deleted_ids  # canlı playlist korundu
    assert result["playlists_deleted"] == 1


def test_me_id_yoksa_hicbir_playlist_silinmez(monkeypatch):
    """GÜVENLİK KAPISI 1: me_id alınamadıysa sahiplik filtresi uygulanmadı,
    remote set güvenilmez. Körü körüne silmek 178 canlı playlist'i yok edebilir
    (§1.5). me_id None iken reconcile ÇALIŞMAMALI."""
    monkeypatch.setattr(runner, "get_valid_spotify_token", lambda c, u, k, h: "tok")
    monkeypatch.setattr(runner, "_fetch_me_id", lambda tok, h, **kw: None)  # /me alınamadı
    monkeypatch.setattr(runner, "fetch_user_playlists", lambda tok, h: [
        {"spotify_id": "pl1", "name": "P1", "snapshot_id": "ayni",
         "track_count": 1, "owner_id": None},
    ])
    monkeypatch.setattr(runner, "fetch_playlist_items", lambda *a, **k: [])

    client = _WriteClient(
        connections=[{"user_id": "u1", "access_token": "enc"}],
        existing_playlists=[
            {"id": "pl-db-1", "platform_id": "pl1", "snapshot_id": "ayni"},
            {"id": "pl-db-2", "platform_id": "pl2", "snapshot_id": "eski"},
        ],
    )
    result = runner.run_one_playlist_refresh(client, http=object(), crypto_key="key")

    assert client.playlists_deleted_ids == []  # HİÇBİR şey silinmedi
    assert result["playlists_deleted"] == 0


def test_remote_bos_donerse_hicbir_playlist_silinmez(monkeypatch):
    """GÜVENLİK KAPISI 2: fetch_user_playlists boş döndüyse API geçici arıza
    vermiş olabilir. Boş sete güvenip her şeyi silmek felaket. Reconcile boş
    remote'ta ÇALIŞMAMALI — DB satırları korunur."""
    monkeypatch.setattr(runner, "get_valid_spotify_token", lambda c, u, k, h: "tok")
    monkeypatch.setattr(runner, "_fetch_me_id", lambda tok, h, **kw: "me-123")
    monkeypatch.setattr(runner, "fetch_user_playlists", lambda tok, h: [])  # boş!
    monkeypatch.setattr(runner, "fetch_playlist_items", lambda *a, **k: [])

    client = _WriteClient(
        connections=[{"user_id": "u1", "access_token": "enc"}],
        existing_playlists=[
            {"id": "pl-db-1", "platform_id": "pl1", "snapshot_id": "ayni"},
            {"id": "pl-db-2", "platform_id": "pl2", "snapshot_id": "eski"},
        ],
    )
    result = runner.run_one_playlist_refresh(client, http=object(), crypto_key="key")

    assert client.playlists_deleted_ids == []  # boş remote'a güvenilmedi
    assert result["playlists_deleted"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# S3 (2026-07-29): Kullanıcı seviyesinde 403 → yalnız o kullanıcı atlanır.
# Canlı olay: Yankı allowlist dışıydı ("The user is not registered for this
# application") ve TÜM cron çöküyordu — Ferzan'ın + Efendim'in playlist'leri
# de tazelenmiyordu. pipeline_runs outcome=error.
# ─────────────────────────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


class _HttpError(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.response = _Resp(status_code)


def test_kullanici_403_verirse_digerleri_islenmeye_devam_eder(monkeypatch):
    """EN KRİTİK: erişilemeyen kullanıcı (allowlist dışı) tüm turu KIRMAZ.
    u_bad 403 alır → atlanır; u_ok normal işlenir."""
    monkeypatch.setattr(runner, "get_valid_spotify_token", lambda c, u, k, h: "tok")
    monkeypatch.setattr(runner, "_fetch_me_id", lambda tok, h, **kw: None)

    def fake_list(tok, h):
        # İlk çağrı (u_bad) 403; ikinci çağrı (u_ok) normal liste döner.
        if not fake_list.called:
            fake_list.called = True
            raise _HttpError(403)
        return [{"spotify_id": "pl1", "name": "P1", "snapshot_id": "s1", "track_count": 1}]
    fake_list.called = False
    monkeypatch.setattr(runner, "fetch_user_playlists", fake_list)
    monkeypatch.setattr(runner, "fetch_playlist_items", lambda tok, pid, h: [])

    client = _WriteClient(
        connections=[
            {"user_id": "u_bad", "access_token": "enc"},
            {"user_id": "u_ok", "access_token": "enc"},
        ],
        existing_playlists=[],
    )
    result = runner.run_one_playlist_refresh(client, http=object(), crypto_key="key")

    # u_ok'un playlist'i İŞLENDİ — 403 onu engellemedi
    assert result["playlists_updated"] == 1
    assert result["skipped_users"] == 1
    # Atlama sessizce yutulmadı: panelde görünsün diye partial
    assert result["outcome"] == "partial"


def test_kullanici_401_de_atlanir(monkeypatch):
    """401 (token geçersiz) de tüm turu kırmamalı — aynı guard."""
    monkeypatch.setattr(runner, "get_valid_spotify_token", lambda c, u, k, h: "tok")
    monkeypatch.setattr(runner, "_fetch_me_id", lambda tok, h, **kw: None)
    monkeypatch.setattr(runner, "fetch_user_playlists",
                        lambda tok, h: (_ for _ in ()).throw(_HttpError(401)))

    client = _WriteClient(connections=[{"user_id": "u1", "access_token": "enc"}],
                          existing_playlists=[])
    result = runner.run_one_playlist_refresh(client, http=object(), crypto_key="key")

    assert result["skipped_users"] == 1
    assert result["playlists_updated"] == 0


def test_beklenmedik_hata_yukari_firlar(monkeypatch):
    """500/ağ hatası YUTULMAZ — cron kaydına yansımalı (sessiz başarısızlık yok).
    §1.5: 'success' deyip yutmak gerçeği gizler."""
    monkeypatch.setattr(runner, "get_valid_spotify_token", lambda c, u, k, h: "tok")
    monkeypatch.setattr(runner, "_fetch_me_id", lambda tok, h, **kw: None)
    monkeypatch.setattr(runner, "fetch_user_playlists",
                        lambda tok, h: (_ for _ in ()).throw(_HttpError(500)))

    client = _WriteClient(connections=[{"user_id": "u1", "access_token": "enc"}],
                          existing_playlists=[])
    try:
        runner.run_one_playlist_refresh(client, http=object(), crypto_key="key")
        raise AssertionError("500 yutuldu — yukarı fırlamalıydı")
    except _HttpError as exc:
        assert exc.response.status_code == 500


def test_spotify_user_id_bos_ise_kaydedilir(monkeypatch):
    """Ferzan senaryosu: bağlantı kimlik-yakalama kodundan ÖNCE açıldığı için
    spotify_user_id NULL → her tur boşuna /v1/me atılıyordu. /me başarılıysa
    değer DB'ye yazılmalı (§1.6: gereksiz istek = kota yeme)."""
    monkeypatch.setattr(runner, "get_valid_spotify_token", lambda c, u, k, h: "tok")
    monkeypatch.setattr(runner, "fetch_user_playlists", lambda tok, h: [])

    class _Http:
        def get(self, url, headers=None, **k):
            class _R:
                status_code = 200
                def raise_for_status(self): pass
                def json(self): return {"id": "9vrq6p8v8xevf1lwwj4k213vc"}
            return _R()

    client = _WriteClient(connections=[{"user_id": "u1", "access_token": "enc"}],
                          existing_playlists=[])
    runner.run_one_playlist_refresh(client, http=_Http(), crypto_key="key")

    assert client.connection_updates == [{"spotify_user_id": "9vrq6p8v8xevf1lwwj4k213vc"}]


def test_me_403_ise_spotify_user_id_yazilmaz(monkeypatch):
    """/me 403 verirse (Yankı) kimlik öğrenilemez — yanlış/boş değer YAZILMAZ."""
    monkeypatch.setattr(runner, "get_valid_spotify_token", lambda c, u, k, h: "tok")
    monkeypatch.setattr(runner, "fetch_user_playlists", lambda tok, h: [])

    class _Http:
        def get(self, url, headers=None, **k):
            raise _HttpError(403)

    client = _WriteClient(connections=[{"user_id": "u1", "access_token": "enc"}],
                          existing_playlists=[])
    runner.run_one_playlist_refresh(client, http=_Http(), crypto_key="key")

    assert client.connection_updates == []

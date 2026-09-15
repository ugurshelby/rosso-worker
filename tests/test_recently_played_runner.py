"""recently_played_runner — play_events yazımı + yeni track INSERT + ISRC testleri."""
from app.pipeline import recently_played_runner as runner
from app.services.api_gate import GateDecision


def _gecit_acik():
    """Merkezî geçit izin veriyor (bütçe var, ceza yok).

    Plan 08 (2026-08-05): runner artık `cooldown.is_blocked` yerine
    `api_gate.check` kullanıyor — testler de o sözleşmeyi taklit eder.
    """
    return GateDecision(allowed=True, reason="ok", remaining=299, used=1, budget=300)


def _gecit_kapali(reason="blocked", blocked_seconds=300):
    """Geçit kapalı — istek atılmamalı."""
    return GateDecision(allowed=False, reason=reason, blocked_seconds=blocked_seconds)


class _WriteClient:
    """Fake Supabase client — connections + tracks + play_events izler."""
    def __init__(self, connections, existing_tracks, nearby_play_events=None):
        self._connections = connections
        self._existing_tracks = existing_tracks
        # (user_id, track_id, played_at) satırları — _has_nearby_play sorgusunun
        # "zaten var" cevabı verdiği senaryoları test etmek için (0104).
        self._nearby_play_events = nearby_play_events or []
        self.play_events_written: list[dict] = []
        self.tracks_inserted: list[dict] = []
        self.last_sync_updated: dict[str, str] = {}
        self.deactivated: list[str] = []

    def table(self, name):
        client = self
        class _Q:
            def __init__(self):
                self._filters = {}
                self._payload = None
                self._range = {}
            def select(self, *a, **k): return self
            def eq(self, col, val):
                self._filters[col] = val
                return self
            def gte(self, col, val):
                self._range[(col, "gte")] = val
                return self
            def lte(self, col, val):
                self._range[(col, "lte")] = val
                return self
            def limit(self, *a, **k): return self
            def ilike(self, *a, **k): return self
            def insert(self, payload):
                self._payload = payload
                self._insert = True
                return self
            def upsert(self, payload, **k):
                self._payload = payload
                self._upsert = True
                return self
            def update(self, payload):
                self._payload = payload
                self._update = True
                return self
            def execute(self):
                if name == "platform_connections" and hasattr(self, "_update"):
                    if self._payload.get("is_active") is False:
                        client.deactivated.append(self._filters.get("user_id", ""))
                    if "last_recently_played_sync_at" in self._payload:
                        client.last_sync_updated[self._filters.get("user_id", "")] = \
                            self._payload.get("last_recently_played_sync_at")
                    return type("R", (), {"data": []})()
                if name == "platform_connections":
                    return type("R", (), {"data": client._connections})()
                if name == "tracks" and hasattr(self, "_insert"):
                    new_id = f"new-{len(client.tracks_inserted)}"
                    client.tracks_inserted.append(self._payload)
                    return type("R", (), {"data": [{**self._payload, "id": new_id}]})()
                if name == "tracks":
                    return type("R", (), {"data": client._existing_tracks})()
                if name == "play_events" and self._range:
                    # _has_nearby_play sorgusu: user_id+track_id eşleşen ve
                    # aralığa düşen bir kayıt var mı.
                    uid = self._filters.get("user_id")
                    tid = self._filters.get("track_id")
                    hit = any(
                        u == uid and t == tid
                        for (u, t, _played_at) in client._nearby_play_events
                    )
                    return type("R", (), {"data": [{"id": "x"}] if hit else []})()
                if name == "play_events" and hasattr(self, "_upsert"):
                    client.play_events_written.append(self._payload)
                    return type("R", (), {"data": [self._payload]})()
                return type("R", (), {"data": []})()
        return _Q()


def test_new_track_inserted_with_isrc_and_play_event_written(monkeypatch):
    """Eşleşmeyen track yeni satır olarak eklenir (ISRC dahil), play_event yazılır."""
    monkeypatch.setattr(runner, "get_valid_spotify_token", lambda c, u, k, h: "tok")
    monkeypatch.setattr(runner.api_gate, "check", lambda c, s, **k: _gecit_acik())
    monkeypatch.setattr(runner, "fetch_recently_played", lambda tok, after, h: [
        {"played_at": "2026-07-02T10:00:00Z", "spotify_id": "sp1", "title": "Song",
         "artists": ["Artist"], "isrc": "US1234567", "duration_ms": 180000},
    ])

    client = _WriteClient(
        connections=[{"user_id": "u1", "access_token": "enc", "last_recently_played_sync_at": None}],
        existing_tracks=[],  # eşleşme yok → yeni INSERT
    )

    result = runner.run_one_recently_played_sync(client, http=None, crypto_key="key")

    assert result["outcome"] == "success"
    assert len(client.tracks_inserted) == 1
    assert client.tracks_inserted[0]["isrc"] == "US1234567"
    assert client.tracks_inserted[0]["spotify_id"] == "sp1"
    assert len(client.play_events_written) == 1
    assert client.play_events_written[0]["source"] == "api_realtime"
    # ms_played gerçek track süresinden gelir (Spotify recently-played API bunu vermez,
    # track.duration_ms fallback kullanılır — Fable 5 keşif bulgusu, 2026-07-02)
    assert client.play_events_written[0]["ms_played"] == 180000
    assert client.last_sync_updated["u1"] == "2026-07-02T10:00:00Z"


def test_empty_garbage_item_not_inserted(monkeypatch):
    """Boş title/artists/spotify_id olan item track olarak INSERT edilmez,
    play_event de yazılmaz (P0 çöp bug'ı guard'ı, 2026-07-03)."""
    monkeypatch.setattr(runner, "get_valid_spotify_token", lambda c, u, k, h: "tok")
    monkeypatch.setattr(runner.api_gate, "check", lambda c, s, **k: _gecit_acik())
    monkeypatch.setattr(runner, "fetch_recently_played", lambda tok, after, h: [
        {"played_at": "2026-07-02T10:00:00Z", "spotify_id": None, "title": "",
         "artists": [], "isrc": None, "duration_ms": 0},
    ])

    client = _WriteClient(
        connections=[{"user_id": "u1", "access_token": "enc", "last_recently_played_sync_at": None}],
        existing_tracks=[],
    )
    result = runner.run_one_recently_played_sync(client, http=None, crypto_key="key")

    assert result["outcome"] == "success"
    assert client.tracks_inserted == []       # çöp track INSERT edilmez
    assert client.play_events_written == []   # play_event de yazılmaz


def test_blocked_provider_skips_sync(monkeypatch):
    """Ceza aktifse hiçbir işlem yapılmaz (merkezî geçit kapalı)."""
    monkeypatch.setattr(
        runner.api_gate, "check", lambda c, s, **k: _gecit_kapali("blocked")
    )
    client = _WriteClient(connections=[], existing_tracks=[])
    result = runner.run_one_recently_played_sync(client, http=None, crypto_key="key")
    assert result["outcome"] == "blocked"


def test_butce_dolunca_istek_atilmaz(monkeypatch):
    """Ceza YOKKEN de bütçe bittiyse durulur — asıl koruma bu (plan 08).

    Ölçüm (2026-08-05): Spotify ~400 istekte 429 veriyor ve ceza 23,86 SAAT.
    Yani "429 gelince dur" yetmez, 429'a hiç varmamak gerekir.
    """
    monkeypatch.setattr(
        runner.api_gate, "check", lambda c, s, **k: _gecit_kapali("budget")
    )
    client = _WriteClient(connections=[], existing_tracks=[])
    result = runner.run_one_recently_played_sync(client, http=None, crypto_key="key")
    assert result["outcome"] == "skipped"
    assert result["gate_reason"] == "budget"


def test_401_deactivates_connection(monkeypatch):
    """401 alıp force_refresh de başarısız olunca platform_connections.is_active=false yazılır."""
    from app.services.spotify_recently_played import SpotifyAuthError
    # İlk çağrıda token var, force_refresh çağrısında None döner (yenileme başarısız)
    monkeypatch.setattr(
        runner, "get_valid_spotify_token",
        lambda c, u, k, h, **kw: None if kw.get("force_refresh") else "tok"
    )
    monkeypatch.setattr(runner.api_gate, "check", lambda c, s, **k: _gecit_acik())

    def raise_401(tok, after, h):
        raise SpotifyAuthError()
    monkeypatch.setattr(runner, "fetch_recently_played", raise_401)

    client = _WriteClient(
        connections=[{"user_id": "u1", "access_token": "enc", "last_recently_played_sync_at": None}],
        existing_tracks=[],
    )
    result = runner.run_one_recently_played_sync(client, http=None, crypto_key="key")
    assert result["outcome"] == "error"
    assert result["errors"] == 1
    assert client.deactivated == ["u1"]


def test_401_self_healing_succeeds(monkeypatch):
    """401 alınca force_refresh ile taze token alınıp dinlemeler kurtarılır (Self-Healing)."""
    from app.services.spotify_recently_played import SpotifyAuthError
    # force_refresh çağrıldığında taze token döner
    monkeypatch.setattr(
        runner, "get_valid_spotify_token",
        lambda c, u, k, h, **kw: "fresh_tok" if kw.get("force_refresh") else "old_tok"
    )
    monkeypatch.setattr(runner.api_gate, "check", lambda c, s, **k: _gecit_acik())

    call_count = 0
    def fetch_mock(tok, after, h):
        nonlocal call_count
        call_count += 1
        if tok == "old_tok":
            raise SpotifyAuthError()
        return []  # fresh_tok ile başarılı boş liste
    monkeypatch.setattr(runner, "fetch_recently_played", fetch_mock)

    client = _WriteClient(
        connections=[{"user_id": "u1", "access_token": "enc", "last_recently_played_sync_at": None}],
        existing_tracks=[],
    )
    result = runner.run_one_recently_played_sync(client, http=None, crypto_key="key")
    assert result["outcome"] == "success"
    assert result["errors"] == 0
    assert client.deactivated == []  # Bağlantı pasife ALINMADI!
    assert call_count == 2


def test_403_skips_user_without_deactivating(monkeypatch):
    """403 (allowlist dışı) alınca is_active=false YAPILMAZ (401'den farkı), kullanıcı
    atlanır ama cron çöker gibi görünmez — sessizce 'success' de demez (B3)."""
    from app.services.spotify_recently_played import SpotifyForbiddenError
    monkeypatch.setattr(runner, "get_valid_spotify_token", lambda c, u, k, h: "tok")
    monkeypatch.setattr(runner.api_gate, "check", lambda c, s, **k: _gecit_acik())

    def raise_403(tok, after, h):
        raise SpotifyForbiddenError()
    monkeypatch.setattr(runner, "fetch_recently_played", raise_403)

    client = _WriteClient(
        connections=[{"user_id": "u1", "access_token": "enc", "last_recently_played_sync_at": None}],
        existing_tracks=[],
    )
    result = runner.run_one_recently_played_sync(client, http=None, crypto_key="key")
    assert client.deactivated == []           # 403 token'ı revoke ETMEZ
    assert result["users_processed"] == 0     # işlenmedi ama "başarılı" sayılmadı
    assert result["events_written"] == 0
    # S1: tek kullanıcı 403'lü → outcome 'error', 'success' yalanı bitti
    assert result["outcome"] == "error"
    assert result["errors"] == 1


def test_mixed_403_and_ok_user_reports_partial(monkeypatch):
    """S1: bir kullanıcı 403, diğeri sağlıklı → 'partial' (dürüst karışık sonuç)."""
    from app.services.spotify_recently_played import SpotifyForbiddenError
    monkeypatch.setattr(runner, "get_valid_spotify_token", lambda c, u, k, h: "tok")
    monkeypatch.setattr(runner.api_gate, "check", lambda c, s, **k: _gecit_acik())

    def fetch(tok, after, h):
        if fetch.calls == 0:
            fetch.calls += 1
            raise SpotifyForbiddenError()
        return [{"played_at": "2026-07-11T10:00:00Z", "spotify_id": "sp1", "title": "S",
                 "artists": ["A"], "isrc": None, "duration_ms": 1000}]
    fetch.calls = 0
    monkeypatch.setattr(runner, "fetch_recently_played", fetch)

    client = _WriteClient(
        connections=[
            {"user_id": "u1", "access_token": "enc", "last_recently_played_sync_at": None},
            {"user_id": "u2", "access_token": "enc", "last_recently_played_sync_at": None},
        ],
        existing_tracks=[],
    )
    result = runner.run_one_recently_played_sync(client, http=None, crypto_key="key")
    assert result["outcome"] == "partial"
    assert result["errors"] == 1
    assert result["users_processed"] == 1
    assert len(client.play_events_written) == 1


def test_latest_sync_stamp_is_newest_not_last(monkeypatch):
    """Spotify newest-first döner; damga en YENİ played_at olmalı, listenin son
    (en eski) item'ı değil (B2 — damga geriye yazma bug'ı)."""
    monkeypatch.setattr(runner, "get_valid_spotify_token", lambda c, u, k, h: "tok")
    monkeypatch.setattr(runner.api_gate, "check", lambda c, s, **k: _gecit_acik())
    # API sırası: newest ilk, oldest son
    monkeypatch.setattr(runner, "fetch_recently_played", lambda tok, after, h: [
        {"played_at": "2026-07-10T12:57:00Z", "spotify_id": "sp-new", "title": "New",
         "artists": ["A"], "isrc": None, "duration_ms": 180000},
        {"played_at": "2026-07-10T07:56:00Z", "spotify_id": "sp-old", "title": "Old",
         "artists": ["B"], "isrc": None, "duration_ms": 200000},
    ])
    client = _WriteClient(
        connections=[{"user_id": "u1", "access_token": "enc", "last_recently_played_sync_at": None}],
        existing_tracks=[],
    )
    runner.run_one_recently_played_sync(client, http=None, crypto_key="key")
    # En yeni damga yazılmalı — en eski (07:56) DEĞİL
    assert client.last_sync_updated["u1"] == "2026-07-10T12:57:00Z"


def test_track_insert_conflict_rereads_instead_of_losing(monkeypatch):
    """INSERT çakışırsa (başka tur araya ekledi) None dönmek yerine tekrar okunur,
    play_event yine yazılır — dinleme kaybolmaz (B4)."""
    monkeypatch.setattr(runner, "get_valid_spotify_token", lambda c, u, k, h: "tok")
    monkeypatch.setattr(runner.api_gate, "check", lambda c, s, **k: _gecit_acik())
    monkeypatch.setattr(runner, "fetch_recently_played", lambda tok, after, h: [
        {"played_at": "2026-07-02T10:00:00Z", "spotify_id": "sp-race", "title": "Race",
         "artists": ["A"], "isrc": None, "duration_ms": 150000},
    ])

    # repo: ilk find boş (INSERT'e gider), INSERT patlar, ikinci find dolu döner
    class _RaceRepo:
        def __init__(self):
            self.calls = 0
        def find_by_spotify_id(self, sid):
            self.calls += 1
            return {"id": "existing-id"} if self.calls >= 2 else None

    # tracks INSERT'i exception fırlatan client
    class _ConflictClient(_WriteClient):
        def table(self, name):
            outer = self
            if name == "tracks":
                class _T:
                    def insert(self, payload): self._p = payload; return self
                    def execute(self): raise RuntimeError("duplicate key")
                    def select(self, *a, **k): return self
                    def eq(self, *a, **k): return self
                    def is_(self, *a, **k): return self
                    def update(self, *a, **k): return self
                    def limit(self, *a, **k): return self
                    def ilike(self, *a, **k): return self
                return _T()
            return super().table(name)

    client = _ConflictClient(
        connections=[{"user_id": "u1", "access_token": "enc", "last_recently_played_sync_at": None}],
        existing_tracks=[],
    )
    repo = _RaceRepo()
    monkeypatch.setattr(runner, "SupabaseTrackRepo", lambda c: repo)

    result = runner.run_one_recently_played_sync(client, http=None, crypto_key="key")
    # çakışmaya rağmen play_event yazıldı (kayıp yok)
    assert len(client.play_events_written) == 1
    assert client.play_events_written[0]["track_id"] == "existing-id"


def test_nearby_zip_play_event_skips_duplicate_write(monkeypatch):
    """ZIP-örtüşme koruması (0104): aynı track için ±5sn içinde zaten bir kayıt
    varsa (ZIP export'undan gelmiş olabilir) api_realtime satırı YAZILMAZ —
    Ferzan'da 328 çift kayıt / ~20 saat fazla süre yaratan bug'ın koruması."""
    monkeypatch.setattr(runner, "get_valid_spotify_token", lambda c, u, k, h: "tok")
    monkeypatch.setattr(runner.api_gate, "check", lambda c, s, **k: _gecit_acik())
    monkeypatch.setattr(runner, "fetch_recently_played", lambda tok, after, h: [
        {"played_at": "2026-07-17T10:00:02Z", "spotify_id": "sp1", "title": "Song",
         "artists": ["Artist"], "isrc": None, "duration_ms": 180000},
    ])

    client = _WriteClient(
        connections=[{"user_id": "u1", "access_token": "enc", "last_recently_played_sync_at": None}],
        existing_tracks=[{"id": "existing-track", "spotify_id": "sp1"}],
        # ZIP'ten 2sn önce yazılmış aynı track — pencere içinde
        nearby_play_events=[("u1", "existing-track", "2026-07-17T10:00:00Z")],
    )
    result = runner.run_one_recently_played_sync(client, http=None, crypto_key="key")

    assert client.play_events_written == []
    assert result["outcome"] == "success"


def test_no_nearby_play_event_writes_normally(monkeypatch):
    """Pencere içinde çakışan kayıt yoksa normal yazım devam eder (regresyon)."""
    monkeypatch.setattr(runner, "get_valid_spotify_token", lambda c, u, k, h: "tok")
    monkeypatch.setattr(runner.api_gate, "check", lambda c, s, **k: _gecit_acik())
    monkeypatch.setattr(runner, "fetch_recently_played", lambda tok, after, h: [
        {"played_at": "2026-07-17T10:00:00Z", "spotify_id": "sp1", "title": "Song",
         "artists": ["Artist"], "isrc": None, "duration_ms": 180000},
    ])

    client = _WriteClient(
        connections=[{"user_id": "u1", "access_token": "enc", "last_recently_played_sync_at": None}],
        existing_tracks=[{"id": "existing-track", "spotify_id": "sp1"}],
        nearby_play_events=[],
    )
    result = runner.run_one_recently_played_sync(client, http=None, crypto_key="key")

    assert len(client.play_events_written) == 1
    assert result["outcome"] == "success"


def test_429_sets_cooldown_and_stops_batch(monkeypatch):
    """429 alınca ceza damgası + bütçe kapatma; kalan kullanıcılar işlenmez.

    ⚠ Artık `cooldown.set_cooldown` değil `api_gate.record_429` çağrılır —
    o hem cezayı yazar HEM bütçeyi kapatır. Bütçe kapatılmazsa ceza bitince
    aynı duvara koşulur (ölçülen desen: cover_backfill 06:04'te 81 istekte
    çarpmış, 13:04 ve 20:02'de 0 istekte).
    """
    from app.services.spotify_recently_played import SpotifyRateLimitError
    monkeypatch.setattr(runner, "get_valid_spotify_token", lambda c, u, k, h: "tok")
    monkeypatch.setattr(runner.api_gate, "check", lambda c, s, **k: _gecit_acik())

    def raise_429(tok, after, h):
        raise SpotifyRateLimitError(30.0)
    monkeypatch.setattr(runner, "fetch_recently_played", raise_429)

    kayitlar = []
    monkeypatch.setattr(
        runner.api_gate, "record_429",
        lambda c, scope, ra, reason: kayitlar.append((scope, ra, reason)) or 30,
    )

    client = _WriteClient(
        connections=[{"user_id": "u1", "access_token": "enc", "last_recently_played_sync_at": None}],
        existing_tracks=[],
    )
    runner.run_one_recently_played_sync(client, http=None, crypto_key="key")

    assert len(kayitlar) == 1, "429'da tam bir kez damga vurulmalı"
    scope, retry_after, reason = kayitlar[0]
    assert scope == "spotify:user", "merkezî geçidin kapsamı kullanılmalı"
    assert retry_after == 30.0, "Spotify'ın istediği süre AYNEN geçirilmeli"
    assert reason == "recently_played_429"

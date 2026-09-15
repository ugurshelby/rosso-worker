"""artist_image_backfill_runner — sanatçı görseli köprüsü + §1.6 rate-limit kapıları.

Köprü İKİ istek: /tracks/{bridge} → ada eşleşen artist.id → /artists/{id} → görsel.
Testler: doldurma, yanlış-görsel koruması (ad eşleşmezse yazma), 429 durması,
ortak cooldown damgası, bloklu havuzda hiç dokunmama.
"""
from app.pipeline import artist_image_backfill_runner as runner


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
    def raise_for_status(self):
        pass
    def json(self):
        return self._payload


class _BridgeHttp:
    """/tracks/{id} → artists listesi; /artists/{id} → images. İstek sayar."""
    def __init__(self, track_artists, artist_images):
        self.track_artists = track_artists      # /tracks yanıtındaki artists[]
        self.artist_images = artist_images      # /artists yanıtındaki images[]
        self.get_calls = 0
    def get(self, url, **kwargs):
        self.get_calls += 1
        if "/tracks/" in url:
            return _FakeResp({"artists": self.track_artists})
        # /artists/{id}
        return _FakeResp({"images": self.artist_images})


class _Client:
    # Bkz. `test_cover_backfill_runner.py` — varsayılan Kat-1 kuyruğu
    # (`paket_gorsel_adaylari_sanatci`, migration 0221).
    _ADAY_RPCLERI = ("paket_gorsel_adaylari_sanatci", "artist_image_backfill_candidates")

    def __init__(self, candidates):
        self._candidates = candidates   # aday RPC sonucu
        self.updates = []               # (id, image_url)
        self.rpc_cagrilari = []
    def rpc(self, name, params=None):
        client = self
        client.rpc_cagrilari.append(name)
        class _R:
            def execute(self):
                if name in _Client._ADAY_RPCLERI:
                    return type("R", (), {"data": client._candidates})()
                return type("R", (), {"data": []})()
        return _R()
    def table(self, name):
        client = self
        class _Q:
            def __init__(self):
                self._payload = None
                self._eq = {}
            def update(self, payload):
                self._payload = payload
                return self
            def eq(self, col, val):
                self._eq[col] = val
                return self
            def execute(self):
                if name == "artists" and self._payload is not None:
                    client.updates.append((self._eq.get("id"), self._payload.get("image_url")))
                    return type("R", (), {"data": [{}]})()
                return type("R", (), {"data": []})()
        return _Q()


class _Settings:
    spotify_client_id = "id"
    spotify_client_secret = "secret"


def _patch_token(monkeypatch, token="tok"):
    monkeypatch.setattr(runner, "_get_access_token", lambda *a, **k: token)


def _patch_cooldown(monkeypatch, blocked=False):
    calls = {"set": []}
    monkeypatch.setattr(runner.cooldown, "is_blocked", lambda c, p: (blocked, 3600 if blocked else 0))
    monkeypatch.setattr(
        runner.cooldown, "set_cooldown",
        lambda c, p, s, reason=None: calls["set"].append((p, s, reason)),
    )
    return calls


def _cand(id_, name, bridge="sp1"):
    return {"id": id_, "name": name, "bridge_track_spotify_id": bridge}


def test_bos_sanatci_gorseli_doldurulur(monkeypatch):
    _patch_token(monkeypatch)
    _patch_cooldown(monkeypatch)
    monkeypatch.setattr(runner, "_with_retry", lambda fn, *a, **k: fn())
    client = _Client([_cand("a1", "Post Malone")])
    http = _BridgeHttp(
        track_artists=[{"id": "spid1", "name": "Post Malone"}],
        artist_images=[{"url": "https://i/pm.jpg", "width": 640}],
    )
    result = runner.run_artist_image_backfill(client, _Settings(), http)

    assert result["outcome"] == "success"
    assert result["updated"] == 1
    assert client.updates == [("a1", "https://i/pm.jpg")]


def test_ad_eslesmezse_gorsel_yazilmaz(monkeypatch):
    """YANLIŞ GÖRSEL KORUMASI (FAZ İ1): track yanıtındaki sanatçılardan adı KESİN
    eşleşen yoksa GÖRSEL YAZMA — yanlış görseli kalıcı yazmaktansa boş bırak."""
    _patch_token(monkeypatch)
    _patch_cooldown(monkeypatch)
    monkeypatch.setattr(runner, "_with_retry", lambda fn, *a, **k: fn())
    client = _Client([_cand("a1", "Aranan Sanatçı")])
    # Köprü track yanlış sanatçıları taşıyor (feat./derleme) — ad eşleşmez.
    http = _BridgeHttp(
        track_artists=[{"id": "spid_x", "name": "Başka Biri"}],
        artist_images=[{"url": "https://i/yanlis.jpg", "width": 640}],
    )
    result = runner.run_artist_image_backfill(client, _Settings(), http)

    assert result["updated"] == 0
    assert result["skipped"] == 1
    assert client.updates == []           # yanlış görsel YAZILMADI


def test_dolduracak_sanatci_yoksa_empty(monkeypatch):
    _patch_token(monkeypatch)
    _patch_cooldown(monkeypatch)
    client = _Client([])
    result = runner.run_artist_image_backfill(client, _Settings(),
                                              _BridgeHttp([], []))
    assert result["outcome"] == "empty"
    assert result["updated"] == 0


def test_429_turu_hemen_durdurur_ve_damga_vurur(monkeypatch):
    """§1.6: SpotifyQuotaExhausted → tur ANINDA durur + ortak 'spotify' cooldown
    damgası. İlk sanatçı yazıldıysa korunur, sonrakilere istek atılmaz."""
    _patch_token(monkeypatch)
    calls = _patch_cooldown(monkeypatch)

    state = {"n": 0}
    def fake_retry(fn, *a, **k):
        state["n"] += 1
        # 1: t1'in /tracks, 2: t1'in /artists (başarılı) → t1 yazılır
        # 3: t2'nin /tracks → kota
        if state["n"] == 3:
            raise runner.SpotifyQuotaExhausted("kota")
        return fn()
    monkeypatch.setattr(runner, "_with_retry", fake_retry)

    client = _Client([
        _cand("a1", "Sanatçı Bir", "sp1"),
        _cand("a2", "Sanatçı İki", "sp2"),
    ])
    http = _BridgeHttp(
        track_artists=[{"id": "spid1", "name": "Sanatçı Bir"}],
        artist_images=[{"url": "https://i/1.jpg", "width": 640}],
    )
    result = runner.run_artist_image_backfill(client, _Settings(), http)

    assert result["quota_hit"] is True
    assert result["outcome"] == "partial"
    assert result["updated"] == 1
    assert client.updates == [("a1", "https://i/1.jpg")]
    # ortak 'spotify' havuzuna damga vuruldu
    assert len(calls["set"]) == 1
    assert calls["set"][0][0] == "spotify"


def test_spotify_bloklu_ise_hic_dokunmaz(monkeypatch):
    """§1.6 ilk kapı: ortak Spotify cooldown aktifse token BİLE alınmaz."""
    token_alindi = {"v": False}
    monkeypatch.setattr(runner, "_get_access_token",
                        lambda *a, **k: token_alindi.__setitem__("v", True))
    _patch_cooldown(monkeypatch, blocked=True)
    client = _Client([_cand("a1", "Post Malone")])
    result = runner.run_artist_image_backfill(client, _Settings(),
                                              _BridgeHttp([], []))
    assert result["outcome"] == "blocked"
    assert result["updated"] == 0
    assert token_alindi["v"] is False


def test_gorsel_yoksa_atlanir(monkeypatch):
    """Sanatçı detayı görselsizse o sanatçı atlanır, hata olmaz."""
    _patch_token(monkeypatch)
    _patch_cooldown(monkeypatch)
    monkeypatch.setattr(runner, "_with_retry", lambda fn, *a, **k: fn())
    client = _Client([_cand("a1", "Post Malone")])
    http = _BridgeHttp(
        track_artists=[{"id": "spid1", "name": "Post Malone"}],
        artist_images=[],   # görsel yok
    )
    result = runner.run_artist_image_backfill(client, _Settings(), http)
    assert result["updated"] == 0
    assert result["skipped"] == 1
    assert client.updates == []


def test_token_yoksa_error(monkeypatch):
    _patch_token(monkeypatch, token=None)
    _patch_cooldown(monkeypatch)
    client = _Client([_cand("a1", "Post Malone")])
    result = runner.run_artist_image_backfill(client, _Settings(),
                                              _BridgeHttp([], []))
    assert result["outcome"] == "error"
    assert result["updated"] == 0


def test_varsayilan_kuyruk_KAT1_dir(monkeypatch):
    """Plan 08 §4 — sanatçı tarafı. Bkz. test_cover_backfill_runner.py.

    Kör kuyruk ile Kat-1 aynı şekli döndürdüğü için varsayılanın sessizce
    geri dönmesi HİÇBİR testi kırmazdı; bu test tam olarak onu yakalar.
    """
    _patch_token(monkeypatch)
    _patch_cooldown(monkeypatch)
    monkeypatch.setattr(runner, "_with_retry", lambda fn, *a, **k: fn())

    client = _Client([_cand("a1", "Post Malone")])
    http = _BridgeHttp(
        track_artists=[{"id": "spid1", "name": "Post Malone"}],
        artist_images=[{"url": "https://i/pm.jpg", "width": 640}],
    )
    runner.run_artist_image_backfill(client, _Settings(), http)

    assert "paket_gorsel_adaylari_sanatci" in client.rpc_cagrilari
    assert "artist_image_backfill_candidates" not in client.rpc_cagrilari

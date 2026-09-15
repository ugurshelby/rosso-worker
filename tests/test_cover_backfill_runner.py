"""cover_backfill_runner — kapak dolgusu + §1.6 rate-limit güvenlik kapıları."""
from app.pipeline import cover_backfill_runner as runner


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
    def raise_for_status(self):
        pass
    def json(self):
        return self._payload


class _FakeHttp:
    """Her track için album.images döndürür; istek sayısını sayar."""
    def __init__(self, image_url="https://i/cover.jpg"):
        self.image_url = image_url
        self.get_calls = 0
    def get(self, url, **kwargs):
        self.get_calls += 1
        return _FakeResp({"album": {"images": [{"url": self.image_url, "width": 640}]}})


class _Client:
    # Aday kuyruğunu veren RPC adları. Varsayılan artık Kat-1
    # (`paket_gorsel_adaylari_track`, migration 0221); kör kuyruk elle
    # çalıştırma için duruyor. Sahte ikisini de tanır ki runner hangi
    # kuyrukla çağrılırsa çağrılsın test aynı adayları görsün.
    _ADAY_RPCLERI = ("paket_gorsel_adaylari_track", "cover_backfill_candidates")

    def __init__(self, tracks):
        self._tracks = tracks       # aday RPC'sinin döndüreceği adaylar
        self.updates = []           # (id, image_url)
        self.rpc_cagrilari = []     # hangi RPC'ler çağrıldı (sözleşme testi)
    def rpc(self, name, params=None):
        client = self
        client.rpc_cagrilari.append(name)
        class _R:
            def execute(self):
                if name in _Client._ADAY_RPCLERI:
                    return type("R", (), {"data": client._tracks})()
                return type("R", (), {"data": []})()
        return _R()
    def table(self, name):
        client = self
        class _Q:
            def __init__(self):
                self._is_update = False
                self._payload = None
                self._eq = {}
            def select(self, *a, **k): return self
            def is_(self, *a, **k): return self
            @property
            def not_(self): return self
            def limit(self, *a, **k): return self
            def update(self, payload):
                self._is_update = True
                self._payload = payload
                return self
            def eq(self, col, val):
                self._eq[col] = val
                return self
            def execute(self):
                if name == "tracks" and self._is_update:
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
    """Ortak cooldown'u mock'la: is_blocked + set_cooldown çağrılarını yakala."""
    calls = {"set": []}
    monkeypatch.setattr(runner.cooldown, "is_blocked", lambda c, p: (blocked, 3600 if blocked else 0))
    monkeypatch.setattr(
        runner.cooldown, "set_cooldown",
        lambda c, p, s, reason=None: calls["set"].append((p, s, reason)),
    )
    return calls


def test_bos_kapaklar_doldurulur(monkeypatch):
    _patch_token(monkeypatch)
    _patch_cooldown(monkeypatch)
    monkeypatch.setattr(runner, "_with_retry", lambda fn, *a, **k: fn())
    client = _Client([
        {"id": "t1", "spotify_id": "sp1"},
        {"id": "t2", "spotify_id": "sp2"},
    ])
    http = _FakeHttp()
    result = runner.run_cover_backfill(client, _Settings(), http)

    assert result["outcome"] == "success"
    assert result["updated"] == 2
    assert client.updates == [("t1", "https://i/cover.jpg"), ("t2", "https://i/cover.jpg")]


def test_dolduracak_track_yoksa_empty(monkeypatch):
    _patch_token(monkeypatch)
    _patch_cooldown(monkeypatch)
    client = _Client([])  # image_url NULL + spotify_id dolu track yok
    result = runner.run_cover_backfill(client, _Settings(), _FakeHttp())
    assert result["outcome"] == "empty"
    assert result["updated"] == 0


def test_429_kotasi_turu_hemen_durdurur(monkeypatch):
    """§1.6: SpotifyQuotaExhausted → tur ANINDA durur, o ana kadar yazılanlar korunur.
    İkinci track'e HİÇ istek atılmaz (ceza beslenmesin)."""
    _patch_token(monkeypatch)
    _patch_cooldown(monkeypatch)

    calls = {"n": 0}
    def fake_retry(fn, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return fn()  # ilk track başarılı
        raise runner.SpotifyQuotaExhausted("kota")  # ikincide kota
    monkeypatch.setattr(runner, "_with_retry", fake_retry)

    client = _Client([
        {"id": "t1", "spotify_id": "sp1"},
        {"id": "t2", "spotify_id": "sp2"},
        {"id": "t3", "spotify_id": "sp3"},
    ])
    result = runner.run_cover_backfill(client, _Settings(), _FakeHttp())

    assert result["quota_hit"] is True
    assert result["outcome"] == "partial"
    assert result["updated"] == 1               # yalnız t1 yazıldı
    assert client.updates == [("t1", "https://i/cover.jpg")]
    assert calls["n"] == 2                        # t3'e HİÇ istek atılmadı (döngü kırıldı)


def test_kapak_yoksa_atlanir(monkeypatch):
    """Album images boş dönerse o track atlanır, hata olmaz."""
    _patch_token(monkeypatch)
    _patch_cooldown(monkeypatch)
    monkeypatch.setattr(runner, "_with_retry", lambda fn, *a, **k: fn())

    class _NoImageHttp:
        def get(self, url, **kwargs):
            return _FakeResp({"album": {"images": []}})
    client = _Client([{"id": "t1", "spotify_id": "sp1"}])
    result = runner.run_cover_backfill(client, _Settings(), _NoImageHttp())

    assert result["updated"] == 0
    assert result["skipped"] == 1
    assert client.updates == []


def test_token_yoksa_error(monkeypatch):
    _patch_token(monkeypatch, token=None)
    _patch_cooldown(monkeypatch)
    client = _Client([{"id": "t1", "spotify_id": "sp1"}])
    result = runner.run_cover_backfill(client, _Settings(), _FakeHttp())
    assert result["outcome"] == "error"
    assert result["updated"] == 0


def test_spotify_bloklu_ise_hic_dokunmaz(monkeypatch):
    """§1.6 ilk kapı: ortak Spotify cooldown aktifse token BİLE alınmaz, hiç
    istek atılmaz — diğer Spotify işlerine yol açılır."""
    token_alindi = {"v": False}
    monkeypatch.setattr(runner, "_get_access_token", lambda *a, **k: token_alindi.__setitem__("v", True))
    _patch_cooldown(monkeypatch, blocked=True)  # havuz bloklu

    client = _Client([{"id": "t1", "spotify_id": "sp1"}])
    result = runner.run_cover_backfill(client, _Settings(), _FakeHttp())

    assert result["outcome"] == "blocked"
    assert result["updated"] == 0
    assert token_alindi["v"] is False   # token bile alınmadı


def test_429_ortak_cooldowna_damga_vurur(monkeypatch):
    """§1.6: kota tükenince ORTAK cooldown'a 'spotify' damgası vurulur — aynı
    havuzu kullanan diğer işler (genre fallback, katalog) is_blocked ile durur."""
    _patch_token(monkeypatch)
    calls = _patch_cooldown(monkeypatch)
    monkeypatch.setattr(runner, "_with_retry",
                        lambda fn, *a, **k: (_ for _ in ()).throw(runner.SpotifyQuotaExhausted("kota")))

    client = _Client([{"id": "t1", "spotify_id": "sp1"}])
    result = runner.run_cover_backfill(client, _Settings(), _FakeHttp())

    assert result["quota_hit"] is True
    # ortak 'spotify' provider'ına cooldown damgası vuruldu
    assert len(calls["set"]) == 1
    assert calls["set"][0][0] == "spotify"


def test_cooldown_spotifynin_istedigi_sureyle_vurulur(monkeypatch):
    """REGRESYON (2026-08-01): cooldown SABİT 3600s ile vuruluyordu.

    Canlı olay: Spotify Retry-After=64926s (18 saat) istedi, kod 3600s yazdı.
    Bir saat sonra cron uyandı, Spotify hâlâ cezadaydı → yeni 429 → yeni 3600s.
    10 saat boyunca her saat başı ceza tazelendi (Railway logları).

    Kural: cooldown süresi platformun İSTEDİĞİ süredir. Üst sınırı uygulamak
    cooldown.set_cooldown'ın işi (MAX_COOLDOWN_S) — çağıran onu kısaltmaz.
    """
    _patch_token(monkeypatch)
    calls = _patch_cooldown(monkeypatch)
    monkeypatch.setattr(
        runner, "_with_retry",
        lambda fn, *a, **k: (_ for _ in ()).throw(
            runner.SpotifyQuotaExhausted("kota", retry_after=64926.0)
        ),
    )

    client = _Client([{"id": "t1", "spotify_id": "sp1"}])
    runner.run_cover_backfill(client, _Settings(), _FakeHttp())

    assert len(calls["set"]) == 1
    provider, seconds, reason = calls["set"][0]
    assert provider == "spotify"
    assert seconds == 64926.0, "Retry-After iletilmeli — sabit süre cezayı besler"
    assert reason == "cover_backfill_429"


def test_cooldown_sure_bilinmiyorsa_bir_saat_varsayar(monkeypatch):
    """HTTP/2 kopmasında Retry-After yok (retry_after=None).

    §4.2: süre okunamazsa 1 saat varsayılır. SIFIR varsaymak devre kesiciyi
    işlevsiz bırakır — cron hemen yine dener ve cezayı besler.
    """
    _patch_token(monkeypatch)
    calls = _patch_cooldown(monkeypatch)
    monkeypatch.setattr(
        runner, "_with_retry",
        lambda fn, *a, **k: (_ for _ in ()).throw(
            runner.SpotifyQuotaExhausted("HTTP/2 ConnectionTerminated")
        ),
    )

    client = _Client([{"id": "t1", "spotify_id": "sp1"}])
    runner.run_cover_backfill(client, _Settings(), _FakeHttp())

    assert calls["set"][0][1] == 3600.0


def test_largest_image_en_buyugu_secer():
    """Sıralamaya güvenme — width'e göre seç (playlist deseni)."""
    images = [
        {"url": "kucuk", "width": 64},
        {"url": "buyuk", "width": 640},
        {"url": "orta", "width": 300},
    ]
    assert runner._largest_image(images) == "buyuk"

def test_largest_image_bos():
    assert runner._largest_image([]) is None
    assert runner._largest_image(None) is None


def test_varsayilan_kuyruk_KAT1_dir_kor_kuyruk_DEGIL():
    """Plan 08 §4: kuyruk paketlerin içi olmalı, kör katalog değil.

    ÖLÇÜLDÜ (2026-08-05): kör kuyruk 25.948 track döndürüyordu; runner
    günlerce dolaşıp ceza yiyor, paketlerdeki 49 gerçek eksik bekliyordu.
    Kat-1 kuyruğu aynı işi 49 istekte bitiriyor.

    Bu test varsayılanın sessizce kör kuyruğa dönmesini engeller — ikisi de
    aynı şekli döndürdüğü için böyle bir gerileme HİÇBİR testi kırmazdı.
    """
    client = _Client([{"id": "t1", "spotify_id": "sp1"}])
    runner.run_cover_backfill(client, _Settings(), _FakeHttp())

    assert "paket_gorsel_adaylari_track" in client.rpc_cagrilari
    assert "cover_backfill_candidates" not in client.rpc_cagrilari


def test_kor_kuyruk_elle_gecilebilir():
    """Kör kuyruk SİLİNMEDİ: tek seferlik elle dolum için hâlâ çağrılabilir."""
    client = _Client([{"id": "t1", "spotify_id": "sp1"}])
    runner.run_cover_backfill(
        client, _Settings(), _FakeHttp(), candidates_rpc="cover_backfill_candidates"
    )

    assert "cover_backfill_candidates" in client.rpc_cagrilari

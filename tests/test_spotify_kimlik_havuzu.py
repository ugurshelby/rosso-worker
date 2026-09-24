"""Kimlik grupları — her kullanıcı KENDİ Spotify app'inin kotasıyla çalışır."""
import app.services.spotify_kimlik_havuzu as mod
from app.cron._gruplu import gruplu_calistir
from app.services.spotify_kimlik_havuzu import KimlikGrubu, kimlik_gruplari


class _Q:
    def __init__(self, rows):
        self._rows = rows
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def execute(self):
        return type("R", (), {"data": self._rows})()


class _Client:
    def __init__(self, baglantilar, byoc):
        self._t = {"platform_connections": baglantilar, "spotify_byoc_credentials": byoc}
    def table(self, ad):
        return _Q(self._t[ad])


def _ortam(monkeypatch):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "shared-id")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "shared-secret")
    monkeypatch.setattr(mod, "decrypt_token", lambda ct, key: ct.replace("enc:", ""))


def test_byoc_ve_paylasilan_ayri_gruplanir(monkeypatch):
    _ortam(monkeypatch)
    client = _Client(
        baglantilar=[
            {"user_id": "efendim", "oauth_client_id": "shared-id"},
            {"user_id": "eski", "oauth_client_id": None},          # 0341 öncesi → paylaşılan
            {"user_id": "ali", "oauth_client_id": "ali-app"},
            {"user_id": "veli", "oauth_client_id": "veli-app"},
        ],
        byoc=[
            {"user_id": "ali", "client_id": "ali-app", "client_secret": "enc:ali-sir", "verified_at": "x"},
            {"user_id": "veli", "client_id": "veli-app", "client_secret": "enc:veli-sir", "verified_at": "x"},
        ],
    )
    gruplar = {g.saglayici: g for g in kimlik_gruplari(client, "k")}

    assert set(gruplar) == {"spotify@ali-app", "spotify@veli-app", "spotify"}
    assert gruplar["spotify@ali-app"].user_ids == ["ali"]
    assert gruplar["spotify@ali-app"].client_secret == "ali-sir"
    assert sorted(gruplar["spotify"].user_ids) == ["efendim", "eski"]
    # 🔴 Efendim'in paylaşılan grubu BAŞKASININ kullanıcısını üstlenmez.
    assert "ali" not in gruplar["spotify"].user_ids


def test_dogrulanmamis_byoc_paylasilana_DUSMEZ(monkeypatch):
    _ortam(monkeypatch)
    client = _Client(
        baglantilar=[{"user_id": "ali", "oauth_client_id": "ali-app"}],
        byoc=[{"user_id": "ali", "client_id": "ali-app", "client_secret": "enc:s", "verified_at": None}],
    )
    assert kimlik_gruplari(client, "k") == []


def test_sifresi_cozulemeyen_byoc_atlanir(monkeypatch):
    _ortam(monkeypatch)
    def _patla(ct, key):
        raise ValueError("bozuk")
    monkeypatch.setattr(mod, "decrypt_token", _patla)
    client = _Client(
        baglantilar=[{"user_id": "ali", "oauth_client_id": "ali-app"}],
        byoc=[{"user_id": "ali", "client_id": "ali-app", "client_secret": "x", "verified_at": "t"}],
    )
    assert kimlik_gruplari(client, "k") == []


def test_baglantisiz_kullanici_hicbir_grupta_yok(monkeypatch):
    _ortam(monkeypatch)
    client = _Client(baglantilar=[], byoc=[])
    assert kimlik_gruplari(client, "k") == []


def test_gruplu_calistir_bir_grubun_429u_digerini_durdurmaz(monkeypatch):
    _ortam(monkeypatch)
    client = _Client(
        baglantilar=[
            {"user_id": "efendim", "oauth_client_id": None},
            {"user_id": "ali", "oauth_client_id": "ali-app"},
        ],
        byoc=[{"user_id": "ali", "client_id": "ali-app", "client_secret": "enc:s", "verified_at": "t"}],
    )
    cagrilan = []

    def calistir(grup: KimlikGrubu, kalan: float):
        cagrilan.append(grup.saglayici)
        if grup.saglayici == "spotify@ali-app":
            return {"outcome": "partial", "processed": 3, "updated": 1, "skipped": 0, "quota_hit": True}
        return {"outcome": "success", "processed": 5, "updated": 5, "skipped": 0, "quota_hit": False}

    sonuc = gruplu_calistir(client, object(), calistir, toplam_butce_s=100.0)

    assert sorted(cagrilan) == ["spotify", "spotify@ali-app"]
    assert sonuc["processed"] == 8 and sonuc["updated"] == 6
    assert sonuc["quota_hit"] is True
    assert sonuc["outcome"] == "partial"


def test_gruplu_calistir_bir_grup_patlarsa_digerleri_calisir(monkeypatch):
    _ortam(monkeypatch)
    client = _Client(
        baglantilar=[
            {"user_id": "efendim", "oauth_client_id": None},
            {"user_id": "ali", "oauth_client_id": "ali-app"},
        ],
        byoc=[{"user_id": "ali", "client_id": "ali-app", "client_secret": "enc:s", "verified_at": "t"}],
    )

    def calistir(grup, kalan):
        if grup.saglayici == "spotify@ali-app":
            raise RuntimeError("token reddedildi")
        return {"outcome": "success", "processed": 2, "updated": 2, "skipped": 0}

    sonuc = gruplu_calistir(client, object(), calistir, toplam_butce_s=100.0)
    assert sonuc["updated"] == 2
    assert sonuc["outcome"] == "partial"


def test_grup_yoksa_bos_doner(monkeypatch):
    _ortam(monkeypatch)
    sonuc = gruplu_calistir(_Client([], []), object(), lambda g, k: {}, toplam_butce_s=10.0)
    assert sonuc["outcome"] == "empty" and sonuc["groups"] == 0


def test_token_onbellegi_client_id_basina(monkeypatch):
    """Bir app'in token'ı ötekine verilmemeli."""
    import app.services.spotify_lookup as lk

    sayac = {"n": 0}

    class _Resp:
        def __init__(self, tok): self._t = tok
        def raise_for_status(self): pass
        def json(self): return {"access_token": self._t, "expires_in": 3600}

    class _Http:
        def post(self, url, headers=None, data=None, timeout=None):
            sayac["n"] += 1
            return _Resp(f"tok-{sayac['n']}")

    lk._token_cache.clear()
    a1 = lk._get_access_token("app-a", "s", _Http())
    b1 = lk._get_access_token("app-b", "s", _Http())
    a2 = lk._get_access_token("app-a", "s", _Http())  # önbellekten
    assert a1 != b1
    assert a1 == a2
    assert sayac["n"] == 2

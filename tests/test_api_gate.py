"""api_gate — merkezî API geçidi testleri.

⚠ NEDEN BU DOSYA ÖNEMLİ: diğer runner testlerinde geçit MOCK'lanıyor
(`_gecit_acik` fixture'ı). Mock'lanan şeyin gerçekten doğru çalıştığını
kanıtlayan tek yer burası. Geçit sessizce "hep izin ver" davranışına düşerse
ölçülen 23,86 saatlik cezaya geri döneriz ve hiçbir test bunu yakalamaz.

Ölçüm dayanağı (2026-08-05, canlı deney):
  · Spotify kalan kotayı SÖYLEMİYOR → kendi sayacımız şart
  · Sınır HIZ değil HACİM: 398 istekte 429
  · Ceza 85.898 sn = 23,86 SAAT
"""
from app.services import api_gate
from app.services.api_gate import GateDecision


class _FakeClient:
    """Supabase istemcisinin geçit için gereken yüzeyi.

    `rpc_yanitlari`: {rpc_adı: dönecek satır listesi}
    `cagrilar`: yapılan RPC çağrıları (sıra ve argümanlarla) — sınama için.
    """

    def __init__(self, rpc_yanitlari=None, patlat=None):
        self._yanitlar = rpc_yanitlari or {}
        self._patlat = patlat or set()
        self.cagrilar: list[tuple[str, dict]] = []

    def rpc(self, ad, params=None):
        self.cagrilar.append((ad, params or {}))
        client = self

        class _Exec:
            def execute(self_inner):
                if ad in client._patlat:
                    raise RuntimeError(f"{ad} patladı (sınama)")

                class _Res:
                    data = client._yanitlar.get(ad, [])

                return _Res()

        return _Exec()


def _butce(allowed, remaining=100, used=10, budget=300):
    return [{"allowed": allowed, "remaining": remaining, "used": used, "budget": budget}]


# ── Kapı 1: cooldown ────────────────────────────────────────────────────────


def test_ceza_varken_istek_atilmaz(monkeypatch):
    """Ceza aktifse bütçeye HİÇ bakılmaz — token bile alınmaz."""
    monkeypatch.setattr(api_gate.cooldown, "is_blocked", lambda c, p: (True, 1200))
    client = _FakeClient()

    karar = api_gate.check(client, api_gate.SCOPE_CATALOG)

    assert karar.allowed is False
    assert karar.reason == "blocked"
    assert karar.blocked_seconds == 1200
    # Bütçe RPC'si hiç çağrılmamalı: ceza varken sayaç harcamak yanlış olurdu.
    assert client.cagrilar == []


# ── Kapı 2: bütçe ───────────────────────────────────────────────────────────


def test_butce_varsa_izin_verir_ve_tuketir(monkeypatch):
    monkeypatch.setattr(api_gate.cooldown, "is_blocked", lambda c, p: (False, 0))
    client = _FakeClient({"budget_check_and_consume": _butce(True, remaining=250)})

    karar = api_gate.check(client, api_gate.SCOPE_CATALOG, priority=api_gate.PRIORITY_CRITICAL)

    assert karar.allowed is True
    assert karar.remaining == 250
    ad, params = client.cagrilar[0]
    assert ad == "budget_check_and_consume"
    assert params["p_scope"] == api_gate.SCOPE_CATALOG


def test_butce_dolunca_izin_vermez(monkeypatch):
    """ASIL KORUMA: ceza yokken de bütçe bittiyse durulur."""
    monkeypatch.setattr(api_gate.cooldown, "is_blocked", lambda c, p: (False, 0))
    client = _FakeClient({"budget_check_and_consume": _butce(False, remaining=0, used=300)})

    karar = api_gate.check(client, api_gate.SCOPE_CATALOG)

    assert karar.allowed is False
    assert karar.reason == "budget"


def test_butce_sorgusu_patlarsa_KAPALI_taraf_secilir(monkeypatch):
    """Sayaç okunamıyorsa istek ATILMAZ.

    "Bilmiyorsam serbest bırak" demek, ölçülen 23,86 saatlik cezaya davetiye
    çıkarmaktır. Belirsizlikte kapalı taraf güvenlidir.
    """
    monkeypatch.setattr(api_gate.cooldown, "is_blocked", lambda c, p: (False, 0))
    client = _FakeClient(patlat={"budget_check_and_consume"})

    karar = api_gate.check(client, api_gate.SCOPE_CATALOG)

    assert karar.allowed is False
    assert karar.reason == "budget"


def test_tanimsiz_scope_izin_vermez(monkeypatch):
    """RPC boş satır dönerse (scope tabloda yok) geçit kapalıdır."""
    monkeypatch.setattr(api_gate.cooldown, "is_blocked", lambda c, p: (False, 0))
    client = _FakeClient({"budget_check_and_consume": []})

    karar = api_gate.check(client, "spotify:olmayan")

    assert karar.allowed is False
    assert karar.reason == "budget"


# ── Kapı 3: öncelik hiyerarşisi ─────────────────────────────────────────────


def test_butce_azalinca_alt_kat_durur_ust_kat_gecer(monkeypatch):
    """Hiyerarşi: kalan %20 iken LOW durur, CRITICAL geçer.

    Efendim'in kuralı: kullanıcının EKRANDA gördüğü şey arka plan bakımından
    önceliklidir.
    """
    monkeypatch.setattr(api_gate.cooldown, "is_blocked", lambda c, p: (False, 0))
    iadeler = []

    def _client():
        c = _FakeClient({"budget_check_and_consume": _butce(True, remaining=60, budget=300)})
        return c

    # LOW: eşiği 0.60 → kalan oran 0.20 < 0.60 → DURMALI (ve bütçeyi iade etmeli)
    c1 = _client()
    monkeypatch.setattr(api_gate, "refund", lambda c, s, n=1: iadeler.append((s, n)))
    karar_low = api_gate.check(c1, api_gate.SCOPE_CATALOG, priority=api_gate.PRIORITY_LOW)
    assert karar_low.allowed is False
    assert karar_low.reason == "priority"
    assert iadeler, "alt kat geçemedi → ayrılan bütçe İADE edilmeli"

    # CRITICAL: eşiği 0.00 → her hâlükârda geçer
    c2 = _client()
    karar_critical = api_gate.check(
        c2, api_gate.SCOPE_CATALOG, priority=api_gate.PRIORITY_CRITICAL
    )
    assert karar_critical.allowed is True


def test_butce_bolme_sifira_karsi_korumali(monkeypatch):
    """budget=0 iken oran hesabı çökmemeli (sıfıra bölme)."""
    monkeypatch.setattr(api_gate.cooldown, "is_blocked", lambda c, p: (False, 0))
    client = _FakeClient({"budget_check_and_consume": _butce(True, remaining=0, budget=0)})

    karar = api_gate.check(client, api_gate.SCOPE_CATALOG, priority=api_gate.PRIORITY_LOW)

    assert isinstance(karar, GateDecision)  # çökmedi


# ── 429 kaydı ───────────────────────────────────────────────────────────────


def test_record_429_hem_ceza_hem_butce_kapatir(monkeypatch):
    """429 alındıysa günlük kota zaten aşılmış demektir — ikisi birlikte.

    Sayacı olduğu yerde bırakmak, ceza bitince aynı duvara koşmak olurdu
    (ölçülen desen: cover_backfill 06:04'te 81 istekte çarpmış, 13:04 ve
    20:02 turları 0 istekte).
    """
    yazilan = {}

    def _sahte_set_cooldown(c, provider, retry_after_s, reason, **kw):
        yazilan["provider"] = provider
        yazilan["retry_after"] = retry_after_s
        yazilan["reason"] = reason
        return int(retry_after_s)

    monkeypatch.setattr(api_gate.cooldown, "set_cooldown", _sahte_set_cooldown)
    client = _FakeClient({"budget_check_and_consume": _butce(False)})

    api_gate.record_429(client, api_gate.SCOPE_CATALOG, 85898.0, reason="deney_429")

    # Ceza: sağlayıcı adı scope'tan türetilir ('spotify:catalog' → 'spotify')
    assert yazilan["provider"] == "spotify"
    assert yazilan["retry_after"] == 85898.0
    # Bütçe: tavana çekilmeli
    butce_cagrilari = [p for ad, p in client.cagrilar if ad == "budget_check_and_consume"]
    assert butce_cagrilari and butce_cagrilari[0]["p_count"] >= 10_000


def test_record_429_sure_bilinmiyorsa_bir_saat(monkeypatch):
    """Retry-After okunamazsa 1 saat varsayılır (kural §1) — 0 DEĞİL.

    Sıfır varsaymak devre kesiciyi öldürür: cron hemen tekrar dener.
    """
    yazilan = {}
    monkeypatch.setattr(
        api_gate.cooldown, "set_cooldown",
        lambda c, p, ra, reason, **kw: yazilan.setdefault("ra", ra) or 3600,
    )
    client = _FakeClient({"budget_check_and_consume": _butce(False)})

    api_gate.record_429(client, api_gate.SCOPE_USER, None, reason="test")

    assert yazilan["ra"] == 3600.0


# ── Kapsam ayrımı ───────────────────────────────────────────────────────────


def test_scope_provider_ayrimi():
    """Uç bazlı kapsam, cooldown sağlayıcısına doğru çevrilmeli (ölçüm ④)."""
    assert api_gate._provider_of(api_gate.SCOPE_CATALOG) == "spotify"
    assert api_gate._provider_of(api_gate.SCOPE_SEARCH) == "spotify"
    assert api_gate._provider_of(api_gate.SCOPE_USER) == "spotify"
    assert api_gate._provider_of("deezer:genre") == "deezer"

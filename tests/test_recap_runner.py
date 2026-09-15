"""Recap payload üretimi — v2 (FAZ R3).

En kritik davranışlar (ikisi de CANLI veri kaybından doğdu):
  1. Kısmi güncelleme — payload BAŞTAN KURULMAZ (year_extras sessiz silinmesi)
  2. Nöbetçi — eksik kapsam 'success' SAYILMAZ (yanlış rolle 0 satır yazımı)
  3. Kullanıcı listesi DB tarafında (REST ~1000 satır kırpması — Ferzan olayı)
  4. Bir kullanıcının hatası diğerlerini durdurmaz (izolasyon)
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from datetime import datetime, timezone

from app.pipeline.recap_runner import (
    build_cover_payload,
    build_discovery_payload,
    build_manifesto_payload,
    build_peak_day_payload,
    build_streak_payload,
    build_top_artists_payload,
    build_top_tracks_payload,
    is_period_completed,
    refresh_user_recaps,
    run_recap_refresh,
)


# ── KATMAN 6 guard: cari dönem recap üretilmez ───────────────────────────────

def test_gecmis_donem_tamamlanmis_sayilir():
    now = datetime(2026, 7, 25, tzinfo=timezone.utc)
    assert is_period_completed("2025-12-31", now) is True   # geçen yıl
    assert is_period_completed("2026-06-30", now) is True   # geçen ay


def test_cari_donem_tamamlanmamis_sayilir():
    now = datetime(2026, 7, 25, tzinfo=timezone.utc)
    # Bu ayın sonu (Temmuz 2026) henüz gelmedi → cari, üretilmez.
    assert is_period_completed("2026-07-31", now) is False
    # Bu yılın sonu henüz gelmedi.
    assert is_period_completed("2026-12-31", now) is False


def test_bozuk_tarih_guvenli_tarafta_uretmez():
    now = datetime(2026, 7, 25, tzinfo=timezone.utc)
    assert is_period_completed("bozuk-tarih", now) is False


def test_refresh_cari_donemi_atlar():
    """refresh_user_recaps cari dönemi skip eder, geçmişi yazar."""
    now_year = datetime.now(timezone.utc).year
    client = MagicMock()
    # Bir geçmiş, bir cari (bu yıl sonu) dönem.
    client.rpc.return_value.execute.return_value.data = [
        {"period_type": "year", "period_label": "2020",
         "period_start": "2020-01-01", "period_end": "2020-12-31"},
        {"period_type": "year", "period_label": str(now_year),
         "period_start": f"{now_year}-01-01", "period_end": f"{now_year}-12-31"},
    ]
    result = refresh_user_recaps(client, "user-1")
    assert result["skipped_current"] == 1


# ── saf helper ───────────────────────────────────────────────────────────────

def test_kapak_payloadi_yillik_ve_aylik_ayrisir():
    assert build_cover_payload("year", "2025")["cover"]["issue_label"] == "ANNUAL ARCHIVE"
    assert build_cover_payload("month", "Mayıs 2026")["cover"]["issue_label"] == "MONTHLY ISSUE"


def test_kapak_payloadi_gorsel_dosyasi_YAZMAZ():
    """Görsel seçimi UI'da deterministik hash ile yapılır.

    Payload'a yazılsaydı, görsel havuzu değiştiğinde donmuş payload silinmiş
    bir dosyaya işaret ederdi (kırık görsel). Bilinçli olarak dışarıda.
    """
    payload = build_cover_payload("year", "2025")
    assert "art" not in payload["cover"]
    assert "image" not in payload["cover"]
    assert ".webp" not in str(payload)


# ── sahte client ─────────────────────────────────────────────────────────────

class _FakeClient:
    def __init__(
        self,
        user_ids: list[str] | None = None,
        periods: list[dict[str, Any]] | None = None,
        missing: list[str] | None = None,
    ):
        self._user_ids = user_ids if user_ids is not None else ["u1"]
        self._periods = periods if periods is not None else [
            {"period_type": "year", "period_label": "2025",
             "period_start": "2025-01-01", "period_end": "2025-12-31"},
        ]
        self._missing = missing or []
        self.calls: list[tuple[str, Any]] = []
        self.upsert_raises: Exception | None = None
        self.user_ids_raises: Exception | None = None
        # FAZ R1.5 — testler bu üçünü ihtiyaca göre değiştirir.
        self.discovery_rows: list[dict[str, Any]] = [
            {"month_start": "2025-12-01", "new_artists": 34, "new_tracks": 103},
            {"month_start": "2025-06-01", "new_artists": 29, "new_tracks": 76},
        ]
        self.streak_rows: list[dict[str, Any]] = [
            {"streak_days": 122, "streak_start": "2025-01-01",
             "streak_end": "2025-05-02"},
        ]
        self.peak_rows: list[dict[str, Any]] = [
            {"day": "2025-03-15", "total_ms": 13_680_000, "play_count": 80,
             "track_id": "t1", "title": "Yapma N'olursun",
             "artist": "Dolu Kadehi Ters Tut", "image_url": "https://img/a.jpg",
             "plays": 6},
            {"day": "2025-03-15", "total_ms": 13_680_000, "play_count": 80,
             "track_id": "t2", "title": "Sopa - Clup Remix",
             "artist": "Hande Yener", "image_url": None, "plays": 5},
        ]
    def rpc(self, name: str, args: Any):
        self.calls.append((name, args))
        res = MagicMock()

        if name == "recap_real_user_ids":
            if self.user_ids_raises:
                raise self.user_ids_raises
            res.data = self._user_ids
        elif name == "recap_periods_with_data":
            res.data = self._periods
        elif name == "upsert_recap_partial":
            if self.upsert_raises:
                raise self.upsert_raises
            res.data = 1
        elif name == "audit_recap_coverage":
            res.data = [{
                "expected_periods": len(self._periods),
                "stored_periods": len(self._periods) - len(self._missing),
                "missing_labels": self._missing,
            }]
        elif name == "recap_listening_summary":
            res.data = [{"total_ms": 1_449_771_965, "total_tracks": 1263,
                         "total_artists": 493}]
        elif name == "recap_dominant_genre":
            res.data = [{"genre_name": "pop", "play_count": 6094}]
        elif name == "recap_top_artists":
            res.data = [{"artist_name": "Madrigal", "play_count": 433}]
        elif name == "recap_top_tracks":
            # Canlı dönüş şeması: image_url YOK (ölçüldü 2026-07-20).
            res.data = [{"track_id": "t1", "title": "Şarkı",
                         "artist_name": "Madrigal", "play_count": 42}]
        # ── FAZ R1.5 — Kart 5/6/7 (migration 0126) ──
        elif name == "recap_discovery_by_month":
            res.data = self.discovery_rows
        elif name == "recap_longest_streak":
            res.data = self.streak_rows
        elif name == "recap_peak_day":
            res.data = self.peak_rows
        else:
            res.data = []

        res.execute = lambda: res
        return res

    def table(self, name: str):
        return _FakeTable(name, self)


class _FakeTable:
    """artists/tracks görsel sorguları için asgari zincir."""

    def __init__(self, name: str, client: "_FakeClient"):
        self._name = name
        self._client = client

    def select(self, *_a, **_k):
        return self

    def in_(self, *_a, **_k):
        return self

    # FAZ R1.5 — keşif ayı görselleri tekil sorgu kullanıyor (.eq + .limit).
    def eq(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        res = MagicMock()
        if self._name == "artists":
            res.data = [{"name": "Madrigal", "image_url": "https://img/madrigal.jpg"}]
        elif self._name == "tracks":
            res.data = [{"id": "t1", "image_url": "https://img/track.jpg"}]
        else:
            res.data = []
        return res


# ── KORUMA (a): kısmi güncelleme ─────────────────────────────────────────────

def test_yazim_upsert_recap_partial_KULLANIR_build_recap_DEGIL():
    """🔴 Payload baştan kuran bir fonksiyon çağrılmamalı.

    Eski `build_recap` payload'ı sıfırdan kuruyordu → yalnız onu çalıştırmak
    `year_extras`'ı sessizce siliyordu (canlı olay 2026-07-20). Yeni yol
    `jsonb ||` ile birleştiren `upsert_recap_partial`.
    """
    client = _FakeClient()
    refresh_user_recaps(client, "u1")

    names = [n for n, _ in client.calls]
    assert "upsert_recap_partial" in names
    assert "build_recap" not in names, "payload baştan kuruluyor → veri kaybı sınıfı"


def test_yazim_kart_bazli_anahtar_gonderir():
    """Kısmi güncellemenin anlamı: payload KART BAZLI anahtarlar taşır.

    RPC bunları `jsonb ||` ile birleştirdiği için, ileride yeni bir kart
    eklendiğinde mevcut anahtarlar silinmez — üstüne eklenir. Bu testin
    koruduğu şey anahtarların varlığı değil, payload'ın DÜZ değil kart-bazlı
    olması (düz olsaydı kısmi güncelleme anlamsızlaşırdı).
    """
    client = _FakeClient()
    refresh_user_recaps(client, "u1")

    _, args = next((n, a) for n, a in client.calls if n == "upsert_recap_partial")
    assert "cover" in args["p_payload"]
    assert isinstance(args["p_payload"]["cover"], dict)


# ── KORUMA (b): nöbetçi ──────────────────────────────────────────────────────

def test_eksik_kapsam_success_SAYILMAZ():
    """🔴 En kritik test. 'Yazdım' demek yetmez — gerçekten yazıldı mı?

    Eski sistemde fonksiyon yanlış rolle çağrılınca guard CTE'si hata vermeden
    0 satır dönüyordu; cron "success" yazıyordu ama hiçbir şey yazılmamıştı.
    Nöbetçi bu sessizliği kırar (CLAUDE.md §1.5: success ≠ doğru sonuç).
    """
    client = _FakeClient(
        periods=[
            {"period_type": "year", "period_label": "2025",
             "period_start": "2025-01-01", "period_end": "2025-12-31"},
            {"period_type": "year", "period_label": "2024",
             "period_start": "2024-01-01", "period_end": "2024-12-31"},
        ],
        missing=["2024"],  # nöbetçi bir dönemin yazılmadığını görüyor
    )
    out = run_recap_refresh(client)

    assert out["outcome"] == "partial", "eksik kapsam success sayıldı → sessiz kayıp"
    assert out["incomplete_users"] == 1


def test_tam_kapsam_success_doner():
    client = _FakeClient()
    out = run_recap_refresh(client)
    assert out["outcome"] == "success"
    assert out["incomplete_users"] == 0
    assert out["recaps_written"] == 1


def test_nobetci_her_kullanici_icin_calisir():
    client = _FakeClient(user_ids=["u1", "u2", "u3"])
    run_recap_refresh(client)
    audits = [n for n, _ in client.calls if n == "audit_recap_coverage"]
    assert len(audits) == 3


# ── Kullanıcı listesi + izolasyon ────────────────────────────────────────────

def test_kullanici_listesi_DB_tarafinda_alinir():
    """CLAUDE.md §1.65: `.select().limit()` + set() ~1000 satırda sessizce kırpar.

    253k satırlık play_events'te bu, yalnız EN ESKİ kullanıcıların görünmesi
    demekti — Ferzan iki cron'dan da sessizce düşmüştü (2026-07-17).
    """
    client = _FakeClient()
    run_recap_refresh(client)
    assert client.calls[0][0] == "recap_real_user_ids"


def test_kullanici_listesi_alinamazsa_SESSIZCE_devam_ETMEZ():
    """RPC kaybı kırpma bug'ını geri getirir → tur iptal, görünür hata."""
    client = _FakeClient()
    client.user_ids_raises = RuntimeError("rpc down")
    out = run_recap_refresh(client)

    assert out["outcome"] == "error"
    assert "rpc down" in out["error"]
    # Eski `.limit(100000)` yoluna DÜŞMEMELİ.
    assert not any(n == "play_events" for n, _ in client.calls)


def test_bir_kullanicinin_hatasi_digerlerini_durdurmaz():
    client = _FakeClient(user_ids=["u1", "u2"])
    client.upsert_raises = RuntimeError("yazım hatası")
    out = run_recap_refresh(client)

    assert out["users_processed"] == 2, "bir kullanıcı patlayınca tur durmuş"
    assert out["errors"] >= 2
    assert out["outcome"] == "partial"


def test_donem_yoksa_yazim_yapilmaz():
    client = _FakeClient(periods=[])
    stats = refresh_user_recaps(client, "u1")
    assert stats == {"periods": 0, "written": 0, "errors": 0}
    assert not any(n == "upsert_recap_partial" for n, _ in client.calls)


def test_kullanici_yoksa_empty():
    client = _FakeClient(user_ids=[])
    out = run_recap_refresh(client)
    assert out["outcome"] == "empty"


# ── FAZ R1: Kart 2/3/4 payload'ları ──────────────────────────────────────────

def test_manifesto_dakikaya_cevirir():
    """Kart 2 ham ms değil DAKİKA gösterir ("29,367 MINS LOGGED")."""
    client = _FakeClient()
    out = build_manifesto_payload(client, "u1", "2025-01-01T00:00:00Z",
                                  "2025-12-31T23:59:59Z")
    assert out["manifesto"]["minutes"] == 1_449_771_965 // 60000
    assert out["manifesto"]["tracks"] == 1263
    assert out["manifesto"]["artists"] == 493
    assert out["manifesto"]["dominant_genre"] == "pop"


def test_manifesto_veri_yoksa_anahtar_YAZILMAZ():
    """Boş dönemde anahtar hiç yazılmamalı — kısmi güncelleme sayesinde
    eski değer korunur, boş kart basılmaz."""
    client = _FakeClient()
    original = client.rpc

    def _rpc(name: str, args):
        if name == "recap_listening_summary":
            res = MagicMock()
            res.data = [{"total_ms": 0, "total_tracks": 0, "total_artists": 0}]
            res.execute = lambda: res
            return res
        return original(name, args)

    client.rpc = _rpc  # type: ignore[method-assign]
    assert build_manifesto_payload(client, "u1", "a", "b") == {}


def test_top_artists_gorseli_ekler():
    client = _FakeClient()
    out = build_top_artists_payload(client, "u1", "a", "b")
    artist = out["top_artists"][0]
    assert artist["name"] == "Madrigal"
    assert artist["plays"] == 433
    assert artist["image_url"] == "https://img/madrigal.jpg"


def test_top_tracks_kapagi_AYRICA_ceker():
    """🔴 `recap_top_tracks` image_url DÖNDÜRMEZ (canlı ölçüm 2026-07-20).

    Varsayıp r["image_url"] okunsaydı sessizce None yazılırdı — kapaklar hiç
    görünmezdi ve kimse fark etmezdi. Kapak track_id ile ayrıca çekilir.
    """
    client = _FakeClient()
    out = build_top_tracks_payload(client, "u1", "a", "b")
    track = out["top_tracks"][0]
    assert track["title"] == "Şarkı"
    assert track["image_url"] == "https://img/track.jpg", "kapak ayrıca çekilmedi"


def test_tur_payloadi_TUM_kartlari_yazar():
    """Tek turda üretilen kart anahtarlarının TAMAMI.

    Küme eşitliği bilinçli: yeni kart eklenince bu test kırılır ve geliştirici
    listeyi güncellemek zorunda kalır — payload'a sessizce anahtar sızmasını
    (ya da bir kartın sessizce düşmesini) yakalar.
    FAZ R1.5'te 4 → 7 kart, FAZ R1.6'da 7 → 8 (soundscape) oldu; 2026-08-11'de
    Kart 8 (soundscape) ve Kart 9 (mood) kaldırıldı — Efendim: "bu ekranları
    görmek bile istemiyorum, hem web hem app heryerden silinsin."
    """
    client = _FakeClient()
    refresh_user_recaps(client, "u1")

    _, args = next((n, a) for n, a in client.calls if n == "upsert_recap_partial")
    assert set(args["p_payload"].keys()) == {
        "cover", "manifesto", "top_artists", "top_tracks",
        "discovery", "streak", "peak_day",
    }


# ── FAZ R1.5 — Kart 5/6/7 payload'ları (2026-07-21) ──────────────────────────

def test_kesif_payloadi_EN_COK_kesfedilen_ayi_secer():
    """RPC ayları sıralı döndürür; kart yalnız ZİRVE ayı gösterir."""
    c = _FakeClient()
    out = build_discovery_payload(c, "u1", "2025-01-01T00:00:00Z", "2025-12-31T23:59:59Z")
    assert out["discovery"]["month_start"] == "2025-12-01"
    assert out["discovery"]["new_artists"] == 34
    assert out["discovery"]["new_tracks"] == 103


def test_kesif_payloadi_ay_sinirini_DOGRU_hesaplar():
    """🔴 Ay uzunluğu 28/29/30/31 değişir. Sabit 30 gün eklemek Şubat'ta bir
    sonraki aya taşar, Ocak'ta bir gün eksik bırakırdı."""
    c = _FakeClient()
    c.discovery_rows = [{"month_start": "2025-02-01", "new_artists": 5, "new_tracks": 9}]
    build_discovery_payload(c, "u1", "2025-01-01T00:00:00Z", "2025-12-31T23:59:59Z")

    # Ayın top şarkısı için atılan çağrının BİTİŞ sınırı Şubat'ın son anı olmalı.
    top_calls = [a for n, a in c.calls if n == "recap_top_tracks" and a.get("p_limit") == 1]
    assert top_calls, "ay içi top_tracks çağrısı yapılmalı"
    assert top_calls[0]["p_to"].startswith("2025-02-28")


def test_kesif_payloadi_ARALIK_ayinda_yil_dondurur():
    """Aralık → bir sonraki ay Ocak, YIL da artmalı (12+1 = 13 ay yok)."""
    c = _FakeClient()
    c.discovery_rows = [{"month_start": "2025-12-01", "new_artists": 5, "new_tracks": 9}]
    build_discovery_payload(c, "u1", "2025-01-01T00:00:00Z", "2025-12-31T23:59:59Z")
    top_calls = [a for n, a in c.calls if n == "recap_top_tracks" and a.get("p_limit") == 1]
    assert top_calls[0]["p_to"].startswith("2025-12-31")


def test_kesif_payloadi_veri_yoksa_ANAHTAR_YAZILMAZ():
    c = _FakeClient()
    c.discovery_rows = []
    assert build_discovery_payload(c, "u1", "a", "b") == {}


def test_seri_payloadi_gun_ve_TARIH_ARALIGI_dondurur():
    """Kart 'OCT 12 — FEB 11' etiketi istiyor; yalnız gün sayısı yetmez.
    (Mevcut user_streaks RPC'si tarih döndürmediği için 0126 yazıldı.)"""
    c = _FakeClient()
    out = build_streak_payload(c, "u1", "a", "b")
    assert out["streak"]["days"] == 122
    assert out["streak"]["start"] == "2025-01-01"
    assert out["streak"]["end"] == "2025-05-02"


def test_seri_payloadi_TEK_GUN_seri_SAYILMAZ():
    """1 günlük 'seri' seri değildir — kart basılmamalı."""
    c = _FakeClient()
    c.streak_rows = [{"streak_days": 1, "streak_start": "2025-03-01",
                      "streak_end": "2025-03-01"}]
    assert build_streak_payload(c, "u1", "a", "b") == {}


def test_seri_payloadi_veri_yoksa_ANAHTAR_YAZILMAZ():
    c = _FakeClient()
    c.streak_rows = []
    assert build_streak_payload(c, "u1", "a", "b") == {}


def test_zirve_gun_payloadi_gun_bilgisini_ILK_satirdan_alir():
    """RPC gün bilgisini HER satırda tekrarlar (tek sorguda iki iş);
    şarkı listesi tüm satırlardan kurulur."""
    c = _FakeClient()
    out = build_peak_day_payload(c, "u1", "a", "b")
    pd = out["peak_day"]
    assert pd["day"] == "2025-03-15"
    assert pd["minutes"] == 228          # 13.680.000 ms → 228 dk
    assert pd["plays"] == 80
    assert len(pd["tracks"]) == 2
    assert pd["tracks"][0]["title"] == "Yapma N'olursun"
    # Görseli olmayan şarkı listede KALIR (UI fallback gösterir).
    assert pd["tracks"][1]["image_url"] is None


def test_zirve_gun_payloadi_sarkisiz_satiri_atlar():
    """left join yüzünden şarkısı olmayan satır gelebilir — basılmamalı."""
    c = _FakeClient()
    c.peak_rows = [{"day": "2025-03-15", "total_ms": 600_000, "play_count": 3,
                    "track_id": None, "title": None, "artist": None,
                    "image_url": None, "plays": None}]
    out = build_peak_day_payload(c, "u1", "a", "b")
    assert out["peak_day"]["tracks"] == []
    assert out["peak_day"]["minutes"] == 10


def test_zirve_gun_payloadi_veri_yoksa_ANAHTAR_YAZILMAZ():
    c = _FakeClient()
    c.peak_rows = []
    assert build_peak_day_payload(c, "u1", "a", "b") == {}


def test_tur_sonunda_YEDI_kart_anahtari_da_yazilir():
    """Kart 1-7 tek turda üretilir; kısmi güncelleme eskisini bozmaz.

    (2026-08-11'de Kart 8/soundscape ve Kart 9/mood kaldırıldı — 8 → 7 kart.)
    """
    c = _FakeClient()
    refresh_user_recaps(c, "u1")
    payload = [a for n, a in c.calls if n == "upsert_recap_partial"][0]["p_payload"]
    for anahtar in ("cover", "manifesto", "top_artists", "top_tracks",
                    "discovery", "streak", "peak_day"):
        assert anahtar in payload, f"{anahtar} payload'da yok"

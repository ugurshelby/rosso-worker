"""Katalog dolgusu — ISRC + duration_ms + album + release_year (RAPOR-1).

En kritik davranışlar:
  1. Spotify'da BULUNAMAYAN track de işaretlenir → sonsuz retry YOK
  2. Kota tükenince o ana kadar yazılanlar KORUNUR (partial)
  3. Yazım hatası SESSİZCE yutulmaz (B19 dersi)
  4. Kuyruk boşsa hiç Spotify isteği ATILMAZ (maliyet sıfır)
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.pipeline.catalog_backfill import (
    _release_year,
    fetch_batch,
    parse_track,
    run_one_catalog_batch,
)
from app.services.spotify_lookup import SpotifyQuotaExhausted


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Hız kısıtlama gecikmesi testleri YAVAŞLATMASIN.

    `fetch_batch` istekler arasında gerçekten bekliyor (Spotify cezası yüzünden
    şart). Testlerde bu 14 saniyeye mal oluyordu → sahte sleep.
    Gecikmenin VARLIĞINI ayrıca `test_istekler_arasinda_gecikme_var` doğruluyor.
    """
    monkeypatch.setattr("app.pipeline.catalog_backfill.time.sleep", lambda _s: None)


# ── saf helper'lar ───────────────────────────────────────────────────────────

def test_release_year_formatlari():
    assert _release_year({"release_date": "2019"}) == 2019
    assert _release_year({"release_date": "2019-05"}) == 2019
    assert _release_year({"release_date": "2019-05-17"}) == 2019
    assert _release_year({"release_date": ""}) is None
    assert _release_year({}) is None
    assert _release_year(None) is None


def test_release_year_sacma_degerleri_yazmaz():
    """Bozuk veri DB'ye girmesin — 1900 öncesi kayıtlı müzik yok."""
    assert _release_year({"release_date": "0000"}) is None
    assert _release_year({"release_date": "9999"}) is None
    assert _release_year({"release_date": "abcd"}) is None


def test_parse_track_dort_alani_da_cikarir():
    """ISRC + duration_ms + album + release_year — dördü de AYNI yanıttan."""
    row = parse_track({
        "id": "abc123",
        "external_ids": {"isrc": "TRABC1900001"},
        "duration_ms": 213_000,
        "album": {"name": "Nowadays", "release_date": "2019-05-17"},
    })
    assert row == {
        "spotify_id": "abc123",
        "isrc": "TRABC1900001",
        "duration_ms": 213_000,
        "album": "Nowadays",
        "release_year": 2019,
    }


def test_parse_track_eksik_alanlari_none_birakir():
    """Spotify bir alanı vermezse None — RPC COALESCE ile mevcut değeri korur."""
    row = parse_track({"id": "x", "duration_ms": 1000})
    assert row["isrc"] is None
    assert row["album"] is None
    assert row["duration_ms"] == 1000


def test_parse_track_gecersiz_girdi():
    assert parse_track(None) is None
    assert parse_track({}) is None


# ── fetch_batch ──────────────────────────────────────────────────────────────

def _http_single(found: dict[str, dict[str, Any]]) -> Any:
    """TEKİL uç taklidi: /v1/tracks/{id}. `found`'da olmayan id → 404.

    ⚠ Toplu uç (/v1/tracks?ids=) Development Mode'da **403 Forbidden** verir
    (canlı kanıt, 2026-07-12). Bu yüzden tekil uç kullanılıyor.
    """
    http = MagicMock()

    def _get(url: str, **_: Any) -> Any:
        track_id = url.rsplit("/", 1)[-1]
        resp = MagicMock()
        if track_id in found:
            resp.status_code = 200
            resp.json.return_value = found[track_id]
            resp.raise_for_status.return_value = None
        else:
            resp.status_code = 404  # silinmiş/bölgesel
        return resp

    http.get.side_effect = _get
    return http


def test_fetch_batch_bulunamayan_track_de_isaretlenir():
    """KRİTİK: Spotify 404 dönerse o id yine listede olmalı.

    Yoksa `catalog_backfill_at` yazılmaz → track her turda tekrar denenir →
    kuyruk HİÇ boşalmaz (sonsuz döngü).
    """
    http = _http_single({
        "var": {"id": "var", "external_ids": {"isrc": "AAA"}, "duration_ms": 1,
                "album": {"name": "A", "release_date": "2020"}},
        # "yok" kasten YOK → 404
    })

    rows = fetch_batch(["var", "yok"], "token", http)
    ids = {r["spotify_id"] for r in rows}

    assert ids == {"var", "yok"}, "bulunamayan id listede YOK → sonsuz retry olur"
    yok = next(r for r in rows if r["spotify_id"] == "yok")
    assert yok["isrc"] is None and yok["duration_ms"] is None


def test_fetch_batch_TEKIL_uc_kullanir_batch_DEGIL():
    """🔴 Toplu uç Development Mode'da 403 verir — her track TEKİL çekilmeli.

    ESKİ TEST YANLIŞ GERÇEĞİ KORUYORDU: "50'lik tek istek atmalı" diyordu ve
    GEÇİYORDU — ama o tek istek canlıda **403 Forbidden** alıyordu. Cron ilk
    turunda patladı.

    Canlı kanıt (aynı 3 id):
        /v1/tracks?ids=a,b,c  → 403 Forbidden
        /v1/tracks/{id} × 3   → 200 OK, ISRC + duration geldi

    Rapor "batch çalışıyor" demişti; ölçüm aksini gösterdi.
    """
    ids = [f"id{i}" for i in range(50)]
    http = _http_single({})  # hepsi 404 — burada önemli olan ÇAĞRI BİÇİMİ

    fetch_batch(ids, "tok", http)

    # Her track için AYRI istek (50 tane), toplu DEĞİL.
    assert http.get.call_count == 50

    # URL yol parametresi taşımalı; `?ids=` sorgusu OLMAMALI.
    for call in http.get.call_args_list:
        url = call.args[0] if call.args else call.kwargs["url"]
        assert "/v1/tracks/" in url, "toplu uç kullanılıyor → 403 alır"
        assert "ids" not in call.kwargs.get("params", {})


# ── run_one_catalog_batch ────────────────────────────────────────────────────

class _FakeQuery:
    """supabase-py akıcı zinciri: .select().is_().not_.is_().or_().order().limit()"""

    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    def select(self, *_a, **_k): return self
    def is_(self, *_a, **_k): return self
    def or_(self, *_a, **_k): return self
    def order(self, *_a, **_k): return self
    def limit(self, *_a, **_k): return self

    # `.not_` bir METOT değil, PROPERTY (supabase-py: client.not_.is_(...))
    @property
    def not_(self):
        return self

    def execute(self):
        res = MagicMock()
        res.data = self._rows
        return res


class _FakeClient:
    def __init__(self, queue: list[list[dict[str, Any]]]):
        self._queue = queue
        self.rpc_calls: list[tuple[str, Any]] = []
        self.rpc_raises: Exception | None = None

    def table(self, _name: str):
        rows = self._queue.pop(0) if self._queue else []
        return _FakeQuery(rows)

    def rpc(self, name: str, args: Any):
        self.rpc_calls.append((name, args))
        if self.rpc_raises:
            raise self.rpc_raises
        res = MagicMock()
        res.data = len(args.get("p_rows", []))
        res.execute = lambda: res
        return res


class _Settings:
    spotify_client_id = "cid"
    spotify_client_secret = "csec"


def test_kuyruk_bossa_hic_spotify_istegi_atilmaz(monkeypatch):
    """Maliyet sıfır: dolgu bitince cron sonsuza kadar çalışsa da istek atmaz."""
    monkeypatch.setattr(
        "app.pipeline.catalog_backfill._get_access_token", lambda *a, **k: "tok"
    )
    called = {"n": 0}
    def _fetch(*_a, **_k):
        called["n"] += 1
        return []
    monkeypatch.setattr("app.pipeline.catalog_backfill.fetch_batch", _fetch)

    client = _FakeClient(queue=[[]])  # boş kuyruk
    out = run_one_catalog_batch(client, _Settings(), max_batches=8)

    assert out["outcome"] == "empty"
    assert called["n"] == 0, "kuyruk boşken Spotify'a gidilmemeli"


def test_kota_tukenince_yazilanlar_korunur(monkeypatch):
    """Kota bitince tur `partial` biter — o ana kadarki iş ÇÖPE ATILMAZ."""
    monkeypatch.setattr(
        "app.pipeline.catalog_backfill._get_access_token", lambda *a, **k: "tok"
    )
    calls = {"n": 0}

    def _fetch(ids, _tok, _http):
        calls["n"] += 1
        if calls["n"] == 1:
            return [{"spotify_id": i, "isrc": "X", "duration_ms": 1,
                     "album": None, "release_year": None} for i in ids]
        raise SpotifyQuotaExhausted("429 cap aşıldı")

    monkeypatch.setattr("app.pipeline.catalog_backfill.fetch_batch", _fetch)

    client = _FakeClient(queue=[
        [{"spotify_id": "a"}, {"spotify_id": "b"}],
        [{"spotify_id": "c"}],
    ])
    out = run_one_catalog_batch(client, _Settings(), max_batches=8)

    assert out["outcome"] == "partial"
    assert out["error"] == "quota_exhausted"
    assert out["updated"] == 2, "ilk batch'in yazımı korunmalı"

    names = [name for name, _ in client.rpc_calls]

    # İlk batch'in yazımı yapıldı.
    assert "apply_catalog_backfill" in names

    # 🔴 KRİTİK: 429'da cooldown DB'ye YAZILMALI.
    # Yazılmazsa cron 10 dk sonra yine dener, yine 429 yer ve Spotify'ın cezasını
    # BESLER. Canlı olay (2026-07-12): 6,4 SAATLİK ceza tam bu yüzden büyüdü —
    # bu cron cooldown'ı hiç kullanmıyordu.
    assert "cooldown_set" in names, "429'da cooldown yazılmadı → ceza büyür"


def test_yazim_hatasi_sessizce_yutulmaz(monkeypatch):
    """B19 dersi: sessiz catch bug'ı gizler. Yazım patlarsa outcome=error."""
    monkeypatch.setattr(
        "app.pipeline.catalog_backfill._get_access_token", lambda *a, **k: "tok"
    )
    monkeypatch.setattr(
        "app.pipeline.catalog_backfill.fetch_batch",
        lambda ids, *_a: [{"spotify_id": i, "isrc": None, "duration_ms": None,
                           "album": None, "release_year": None} for i in ids],
    )

    client = _FakeClient(queue=[[{"spotify_id": "a"}]])
    client.rpc_raises = RuntimeError("db down")

    out = run_one_catalog_batch(client, _Settings(), max_batches=8)

    assert out["outcome"] == "error"
    assert "db down" in out["error"]


def test_token_yoksa_dogru_hata(monkeypatch):
    monkeypatch.setattr(
        "app.pipeline.catalog_backfill._get_access_token", lambda *a, **k: None
    )
    client = _FakeClient(queue=[[{"spotify_id": "a"}]])
    out = run_one_catalog_batch(client, _Settings(), max_batches=1)
    assert out["outcome"] == "error"
    assert out["error"] == "no_token"


def test_basarili_tur_dogru_sayar(monkeypatch):
    monkeypatch.setattr(
        "app.pipeline.catalog_backfill._get_access_token", lambda *a, **k: "tok"
    )
    monkeypatch.setattr(
        "app.pipeline.catalog_backfill.fetch_batch",
        lambda ids, *_a: [{"spotify_id": i, "isrc": "I", "duration_ms": 2,
                           "album": "A", "release_year": 2020} for i in ids],
    )
    client = _FakeClient(queue=[
        [{"spotify_id": "a"}, {"spotify_id": "b"}],
        [],  # kuyruk bitti
    ])
    out = run_one_catalog_batch(client, _Settings(), max_batches=8)

    assert out["outcome"] == "success"
    assert out["processed"] == 2
    assert out["updated"] == 2


# ── Devre kesici + hız kısıtlama (canlı olay 2026-07-12) ────────────────────
#
# 🔴 Tekil uca geçince cron 50 isteği ARKA ARKAYA attı. Spotify uygulamayı
#    cezalandırdı: Retry-After = 22.881 sn (6,4 SAAT). Tek yavaş istek bile
#    429 alır oldu.
#
#    Kök neden ikili:
#      1. İstekler arası gecikme YOKTU (50 kat trafik, toplu uca kıyasla)
#      2. Bu cron cooldown'ı HİÇ KULLANMIYORDU — altyapı vardı ama bu dosya
#         ondan habersizdi → her 10 dk yine deniyor, cezayı besliyordu.

def test_cooldown_aktifken_HIC_istek_atilmaz(monkeypatch):
    """Devre kesici açıkken Spotify'a dokunulmamalı — ceza büyümesin."""
    monkeypatch.setattr(
        "app.services.cooldown.is_blocked", lambda *a, **k: (True, 22881)
    )

    token_calls = {"n": 0}

    def _token(*_a, **_k):
        token_calls["n"] += 1
        return "tok"

    monkeypatch.setattr("app.pipeline.catalog_backfill._get_access_token", _token)

    out = run_one_catalog_batch(_FakeClient(queue=[]), _Settings())

    assert out["outcome"] == "blocked"
    assert out["processed"] == 0
    assert out["cooldown_remaining_s"] == 22881
    # Token bile ALINMAMALI: bloklu isek Spotify'a HİÇ dokunma.
    assert token_calls["n"] == 0, "cooldown aktifken Spotify'a istek atıldı"


def test_retry_after_mesajdan_okunur():
    """Cooldown süresi Retry-After'dan gelmeli — sabit tahmin değil."""
    from app.pipeline.catalog_backfill import _retry_after_seconds

    assert _retry_after_seconds(
        SpotifyQuotaExhausted("Retry-After=22881.0s > cap=60.0s")
    ) == 22881.0


def test_retry_after_okunamazsa_1_saat_varsayilir():
    """Süre yoksa SIFIR varsaymak devre kesiciyi işlevsiz bırakır.

    Sıfır → cron hemen yine dener → cezayı besler. Fazla beklemek, az
    beklemekten iyidir.
    """
    from app.pipeline.catalog_backfill import _retry_after_seconds

    assert _retry_after_seconds(
        SpotifyQuotaExhausted("HTTP/2 ConnectionTerminated: baglanti kesildi")
    ) == 3600.0


def test_istekler_arasinda_gecikme_var(monkeypatch):
    """İstekleri arka arkaya atmak 6,4 saatlik cezaya mal oldu — gecikme ŞART."""
    sleeps: list[float] = []
    monkeypatch.setattr("app.pipeline.catalog_backfill.time.sleep", sleeps.append)

    http = _http_single({})  # hepsi 404 — burada önemli olan GECİKME
    fetch_batch(["a", "b", "c"], "tok", http)

    # 3 istek → 2 bekleme (ilkinden önce beklenmez).
    assert len(sleeps) == 2
    assert all(s > 0 for s in sleeps)

"""ISRC dolgusu (Deezer öncelikli) — FAZ ISRC-D.

En kritik davranışlar:
  1. Yanlış sanatçının track'i ASLA kabul edilmez (Stabil→Ati242 sınıfı)
  2. Bulunamayan track de damgalanır → sonsuz retry YOK
  3. Kota (429/code=4) → cooldown DB'ye yazılır, tamamlanan iş korunur
  4. Kuyruk boşsa hiç Deezer isteği atılmaz (maliyet sıfır)
  5. Cooldown aktifken SIFIR istek (devre kesici — Spotify 6,4 saat dersi)
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.pipeline.isrc_backfill import (
    _pick_best,
    _release_year,
    lookup_isrc,
    parse_deezer_track,
    run_one_isrc_batch,
    strip_decorations,
)
from app.services.genre_errors import RateLimitError


@pytest.fixture(autouse=True)
def _no_pace(monkeypatch):
    """_paced_get'in küresel 250ms bekleme kilidi testleri yavaşlatmasın."""
    monkeypatch.setattr("app.services.deezer_genre._PACE_GAP_S", 0.0)


# ── saf helper'lar ───────────────────────────────────────────────────────────

def test_release_year_formatlari():
    assert _release_year("2019-05-17") == 2019
    assert _release_year("2019") == 2019
    assert _release_year("") is None
    assert _release_year(None) is None
    assert _release_year("0000") is None
    assert _release_year("abcd") is None


def test_parse_deezer_track_alanlari_cikarir():
    """Deezer süreyi SANİYE verir → ms'e çevrilmeli; ISRC upper'lanmalı."""
    row = parse_deezer_track({
        "id": 123,
        "isrc": "tr0010600116",
        "duration": 198,
        "album": {"title": "Festival"},
        "release_date": "2006-06-13",
    })
    assert row == {
        "isrc": "TR0010600116",
        "duration_ms": 198_000,
        "album": "Festival",
        "release_year": 2006,
    }


def test_parse_deezer_track_eksikler_none():
    row = parse_deezer_track({"id": 1, "isrc": "", "duration": 0})
    assert row["isrc"] is None
    assert row["duration_ms"] is None
    assert row["album"] is None
    assert parse_deezer_track(None) is None
    assert parse_deezer_track({}) is None


# ── _pick_best doğrulaması ───────────────────────────────────────────────────

def _hit(artist: str, title: str, tid: int = 1) -> dict[str, Any]:
    return {"id": tid, "title": title, "artist": {"name": artist}}


def test_pick_best_yanlis_sanatciyi_reddeder():
    """KRİTİK: Deezer ilk sonucu alakasız olabilir — yanlış ISRC yazmak,
    hiç yazmamaktan KÖTÜDÜR (eşleşme motoru yanlış şarkıya kilitlenir)."""
    assert _pick_best([_hit("Ati242", "Yağmur")], "Stabil", "Yağmur") is None


def test_pick_best_yanlis_basligi_reddeder():
    assert _pick_best([_hit("Hidra", "Bambaşka Bir Şarkı")], "Hidra", "Var") is None


def test_pick_best_fold_toleransi():
    """İ/aksan farkı eşleşmeyi bozmamalı (İkilem ≈ ikilem, Gülşen ≈ Gulsen)."""
    assert _pick_best([_hit("Ikilem", "Kaybolurum Gulusunde")],
                      "İkilem", "Kaybolurum Gülüşünde") is not None


def test_pick_best_en_benzer_adayi_secer():
    hits = [
        _hit("Sade Adu", "Paradise (Live)", tid=1),
        _hit("Sade", "Paradise", tid=2),
    ]
    best = _pick_best(hits, "Sade", "Paradise")
    assert best is not None and best["id"] == 2


# ── lookup_isrc ──────────────────────────────────────────────────────────────

def _http_deezer(
    search_data: list[dict[str, Any]] | None = None,
    track: dict[str, Any] | None = None,
    plain_search_data: list[dict[str, Any]] | None = None,
) -> Any:
    """Deezer taklidi: /search (A: artist:" içeren sorgu, B: düz) + /track/{id}."""
    http = MagicMock()

    def _get(url: str, **_: Any) -> Any:
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status.return_value = None
        if "/track/" in url:
            resp.json.return_value = track or {}
        elif "artist%3A%22" in url or 'artist:"' in url:
            resp.json.return_value = {"data": search_data or []}
        else:
            resp.json.return_value = {"data": plain_search_data or []}
        return resp

    http.get.side_effect = _get
    return http


def test_lookup_isrc_asama_a_bulur():
    http = _http_deezer(
        search_data=[_hit("Kenan Doğulu", "Çakkıdı", tid=9)],
        track={"id": 9, "isrc": "TR0010600116", "duration": 198,
               "album": {"title": "Festival"}, "release_date": "2006-06-13"},
    )
    row = lookup_isrc("Kenan Doğulu", "Çakkıdı", http)
    assert row is not None and row["isrc"] == "TR0010600116"
    assert row["duration_ms"] == 198_000


def test_lookup_isrc_asama_b_fallback_dogrulamali():
    """Aşama A boş → düz arama; doğrulama orada da zorunlu."""
    http = _http_deezer(
        search_data=[],
        plain_search_data=[_hit("Teoman", "Serseri", tid=5)],
        track={"id": 5, "isrc": "TR0531505505", "duration": 240},
    )
    row = lookup_isrc("Teoman", "Serseri", http)
    assert row is not None and row["isrc"] == "TR0531505505"


def test_lookup_isrc_hicbir_aday_gecemezse_none():
    http = _http_deezer(
        search_data=[_hit("Ati242", "Yağmur")],
        plain_search_data=[_hit("Ati242", "Yağmur")],
    )
    assert lookup_isrc("Stabil", "Yağmur", http) is None


def test_lookup_isrc_sayisal_baslik_hic_istek_atmaz():
    """Tamamen sayısal başlık Deezer ayrıştırıcısını kırıyor (canlı, 2026-07-02)."""
    http = _http_deezer()
    assert lookup_isrc("Sanatçı", "129", http) is None
    assert http.get.call_count == 0


def test_lookup_isrc_kota_govde_hatasi_yukari_firlar():
    """HTTP 200 + {"error":{"code":4}} → RateLimitError (sessiz None DEĞİL)."""
    http = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"error": {"code": 4}}
    http.get.return_value = resp
    with pytest.raises(RateLimitError):
        lookup_isrc("Teoman", "Serseri", http)


# ── run_one_isrc_batch ───────────────────────────────────────────────────────

class _FakeQuery:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    def select(self, *_a, **_k): return self
    def is_(self, *_a, **_k): return self
    def order(self, *_a, **_k): return self
    def limit(self, *_a, **_k): return self

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
        if self.rpc_raises and name == "apply_catalog_backfill":
            raise self.rpc_raises
        res = MagicMock()
        res.data = len(args.get("p_rows", [])) if name == "apply_catalog_backfill" else []
        res.execute = lambda: res
        return res


def _track_row(sid: str, title: str = "Şarkı", artist: str = "Sanatçı") -> dict[str, Any]:
    return {"spotify_id": sid, "title": title, "artists": [artist]}


def test_kuyruk_bossa_hic_deezer_istegi_atilmaz(monkeypatch):
    called = {"n": 0}

    def _lookup(*_a, **_k):
        called["n"] += 1
        return None

    monkeypatch.setattr("app.pipeline.isrc_backfill.lookup_isrc", _lookup)
    out = run_one_isrc_batch(_FakeClient(queue=[[]]))
    assert out["outcome"] == "empty"
    assert called["n"] == 0


def test_bulunamayan_track_de_damgalanir(monkeypatch):
    """KRİTİK: bulunamayan spotify_id de RPC satırında olmalı — yoksa
    catalog_backfill_at yazılmaz, track her turda yeniden denenir (sonsuz döngü)."""
    monkeypatch.setattr(
        "app.pipeline.isrc_backfill.lookup_isrc",
        lambda artist, title, http: (
            {"isrc": "AAA", "duration_ms": 1000, "album": None, "release_year": None}
            if title == "var" else None
        ),
    )
    client = _FakeClient(queue=[
        [_track_row("s1", title="var"), _track_row("s2", title="yok")],
        [],
    ])
    out = run_one_isrc_batch(client)

    assert out["outcome"] == "success"
    assert out["found"] == 1
    write = next(a for n, a in client.rpc_calls if n == "apply_catalog_backfill")
    ids = {r["spotify_id"] for r in write["p_rows"]}
    assert ids == {"s1", "s2"}, "bulunamayan id satırda YOK → sonsuz retry olur"


def test_kota_yiyince_cooldown_yazilir_ve_is_korunur(monkeypatch):
    """429 → cooldown DB'ye YAZILMALI (Spotify 6,4 saat dersi) ve o ana kadar
    tamamlanan track'ler ÇÖPE ATILMAMALI."""
    calls = {"n": 0}

    def _lookup(artist, title, http):
        calls["n"] += 1
        if calls["n"] <= 1:
            return {"isrc": "X", "duration_ms": 1, "album": None, "release_year": None}
        raise RateLimitError("deezer", 42.0)

    monkeypatch.setattr("app.pipeline.isrc_backfill.lookup_isrc", _lookup)
    client = _FakeClient(queue=[
        [_track_row("a"), _track_row("b"), _track_row("c")],
    ])
    out = run_one_isrc_batch(client)

    assert out["outcome"] == "partial"
    assert out["error"] == "rate_limited"

    names = [n for n, _ in client.rpc_calls]
    assert "cooldown_set" in names, "429'da cooldown yazılmadı → ceza büyür"
    assert "apply_catalog_backfill" in names, "tamamlanan iş yazılmadı"

    # Yarım kalan parti: yalnız TAMAMLANMIŞ track (a) yazılır; b/c damgalanmaz
    # ki sonraki tur onları yeniden denesin.
    write = next(a for n, a in client.rpc_calls if n == "apply_catalog_backfill")
    assert {r["spotify_id"] for r in write["p_rows"]} == {"a"}


def test_cooldown_aktifken_hic_istek_atilmaz(monkeypatch):
    monkeypatch.setattr(
        "app.services.cooldown.is_blocked", lambda *a, **k: (True, 599)
    )
    called = {"n": 0}

    def _lookup(*_a, **_k):
        called["n"] += 1
        return None

    monkeypatch.setattr("app.pipeline.isrc_backfill.lookup_isrc", _lookup)
    out = run_one_isrc_batch(_FakeClient(queue=[[_track_row("a")]]))

    assert out["outcome"] == "blocked"
    assert out["cooldown_remaining_s"] == 599
    assert called["n"] == 0, "devre kesici açıkken Deezer'a istek atıldı"


def test_yazim_hatasi_sessizce_yutulmaz(monkeypatch):
    monkeypatch.setattr(
        "app.pipeline.isrc_backfill.lookup_isrc", lambda *a, **k: None
    )
    client = _FakeClient(queue=[[_track_row("a")]])
    client.rpc_raises = RuntimeError("db down")
    out = run_one_isrc_batch(client)
    assert out["outcome"] == "error"
    assert "db down" in out["error"]


def test_zaman_butcesi_asilinca_tur_kapanir(monkeypatch):
    """Bütçe dolunca kalan parti sonraki cron'a devreder — hata değil."""
    monkeypatch.setattr(
        "app.pipeline.isrc_backfill.lookup_isrc", lambda *a, **k: None
    )
    client = _FakeClient(queue=[[_track_row("a")], [_track_row("b")]])
    out = run_one_isrc_batch(client, time_budget_s=0.0)
    assert out["outcome"] == "success"
    assert out["processed"] == 0  # bütçe 0 → ilk partiye hiç girilmedi


def test_basarili_tur_dogru_sayar(monkeypatch):
    monkeypatch.setattr(
        "app.pipeline.isrc_backfill.lookup_isrc",
        lambda artist, title, http: {"isrc": "I", "duration_ms": 2,
                                     "album": "A", "release_year": 2020},
    )
    client = _FakeClient(queue=[
        [_track_row("a"), _track_row("b")],
        [],
    ])
    out = run_one_isrc_batch(client)
    assert out["outcome"] == "success"
    assert out["processed"] == 2
    assert out["updated"] == 2
    assert out["found"] == 2


# ── Süsleme temizliği FALLBACK (2026-07-21, Efendim kararı) ──────────────────
#
# Canlı sonda (60 örneklem): bulunamayanların %18'i Deezer'da VAR, sanatçı
# skoru 1.00, ama 'feat.' eki başlık benzerliğini eşiğin altına düşürüyor.
# Efendim: "süsleme temizleme FALLBACK olarak eklensin, direkt süslemeyi
# temizleyip denemek de yanlış olur, sıkıntı çıkabilir."

def test_strip_decorations_feat_eki_atilir():
    assert strip_decorations("Gasoline (feat. Taylor Swift)") == "Gasoline"
    assert strip_decorations("Lucky You (feat. Joyner Lucas)") == "Lucky You"
    assert strip_decorations("Instruction (feat. Demi Lovato & Stefflon Don)") == "Instruction"


def test_strip_decorations_coklu_blok_temizlenir():
    """'MUTT (feat. Chris Brown) [CB REMIX]' — iki blok birden."""
    assert strip_decorations("MUTT (feat. Chris Brown) [CB REMIX]") == "MUTT"


def test_strip_decorations_tire_ile_ayrilmis_kuyruk():
    assert strip_decorations("Sen İstanbulsun - Speed Up") == "Sen İstanbulsun"
    assert strip_decorations("NKBİ X YAPAMAM - Remix") == "NKBİ X YAPAMAM"


def test_strip_decorations_soundtrack_eki():
    assert strip_decorations(
        'Still Don\'t Know My Name (From "Euphoria: Season 1" Soundtrack)'
    ) == "Still Don't Know My Name"


def test_strip_decorations_susleme_yoksa_aynen_doner():
    assert strip_decorations("Lavinia") == "Lavinia"
    assert strip_decorations("505") == "505"


def test_strip_decorations_her_sey_silinirse_orijinal_korunur():
    """Başlığın tamamı süslemeyse boş string dönmemeli — arama kırılır."""
    assert strip_decorations("(Live)") == "(Live)"


def test_lookup_isrc_fallback_feat_ekini_atip_bulur():
    """🔴 ASIL KAZANIM: tam başlık hiçbir adayı geçiremez, süslemesiz geçer.

    Deezer'da 'Gasoline' var; bizde 'Gasoline (feat. Taylor Swift)'.
    Aşama A/B tam başlıkla arar → benzerlik 0.43 < 0.60 → red.
    Aşama C süslemesiz arar VE süslemesiz kıyaslar → geçer.
    """
    http = _http_deezer(
        search_data=[_hit("HAIM", "Gasoline", tid=77)],
        plain_search_data=[_hit("HAIM", "Gasoline", tid=77)],
        track={"id": 77, "isrc": "USUM72101234", "duration": 200},
    )
    row = lookup_isrc("HAIM", "Gasoline (feat. Taylor Swift)", http)
    assert row is not None and row["isrc"] == "USUM72101234"


def test_lookup_isrc_fallback_susleme_yoksa_EK_ISTEK_ATMAZ():
    """⚠ Rate-limit koruması (§1.6): süsleme içermeyen başlıkta fallback
    aynı sorguyu tekrar atmamalı — boşuna istek boşuna kota demek.

    'Lavinia' süslemesiz → A + B = 2 istek. Fallback 3.'yü ATMAMALI.
    """
    http = _http_deezer(search_data=[], plain_search_data=[])
    assert lookup_isrc("Dest", "Lavinia", http) is None
    assert http.get.call_count == 2, "süslemesiz başlıkta 3. istek atılmamalı"


def test_lookup_isrc_fallback_tam_baslik_TUTARSA_devreye_girmez():
    """Fallback yalnız son çare. Tam başlık eşleşince süslemesiz arama YAPILMAZ
    — yoksa remix'i orijinaliyle karıştırma riski doğar (Efendim'in kaygısı)."""
    http = _http_deezer(
        search_data=[_hit("Elyas & Taha", "Ay (Original Mix)", tid=31)],
        track={"id": 31, "isrc": "TR1234567890", "duration": 180},
    )
    row = lookup_isrc("Elyas & Taha", "Ay (Original Mix)", http)
    assert row is not None and row["isrc"] == "TR1234567890"
    # A (1 arama) + /track (1) = 2. Düz arama da fallback de çalışmamalı.
    assert http.get.call_count == 2


def test_lookup_isrc_fallback_yanlis_sarkiyi_YINE_reddeder():
    """Süsleme temizliği eşiği GEVŞETMEZ. Sanatçı tutmuyorsa yine None.

    'BARRI' → 'BRING IT BACK!' sınıfı (canlı sondada 0.21) içeri girmemeli.
    """
    http = _http_deezer(
        search_data=[_hit("Baska Sanatci", "BRING IT BACK!")],
        plain_search_data=[_hit("Baska Sanatci", "BRING IT BACK!")],
    )
    assert lookup_isrc("Eddie Fresco", "BARRI (feat. X)", http) is None

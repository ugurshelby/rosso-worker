"""post_import_refresh — ZIP-sonrası anında recap+taste tazeleme + izolasyon.

Kritik: tazeleme HATASI export'u bozmamalı (her hata yutulur). Ve başarı yolunda
hem taste RPC hem recap orkestratörü çağrılmalı.
"""
from app.pipeline import post_import_refresh as mod


class _Rpc:
    def __init__(self, recorder, raise_on=None):
        self._rec = recorder
        self._raise_on = raise_on or set()
    def __call__(self, name, params=None):
        self._rec.append((name, params))
        rpc = self
        class _Exec:
            def execute(_self):
                if name in rpc._raise_on:
                    raise RuntimeError(f"{name} patladı")
                return type("R", (), {"data": []})()
        return _Exec()


class _Client:
    def __init__(self, raise_on=None):
        self.calls = []
        self.rpc = _Rpc(self.calls, raise_on=raise_on)


def test_basari_taste_ve_recap_tetiklenir(monkeypatch):
    # recap orkestratörünü mock'la (gerçek RPC zincirini çalıştırmasın).
    called = {}
    def fake_recap(client, user_id):
        called["user_id"] = user_id
        return {"periods": 3, "written": 3, "errors": 0, "skipped_current": 1}
    monkeypatch.setattr(
        "app.pipeline.recap_runner.refresh_user_recaps", fake_recap, raising=False
    )

    client = _Client()
    result = mod.refresh_after_import(client, "u1")

    assert result["taste"] == "ok"
    assert result["recap"] == "ok"
    assert result["recaps_written"] == 3
    assert called["user_id"] == "u1"
    # taste RPC gerçekten çağrıldı
    assert ("refresh_user_taste", {"p_user_id": "u1"}) in client.calls


def test_taste_patlarsa_recap_yine_denenir(monkeypatch):
    """Taste hatası recap'i engellememeli — ikisi bağımsız."""
    def fake_recap(client, user_id):
        return {"periods": 1, "written": 1, "errors": 0}
    monkeypatch.setattr(
        "app.pipeline.recap_runner.refresh_user_recaps", fake_recap, raising=False
    )

    client = _Client(raise_on={"refresh_user_taste"})
    result = mod.refresh_after_import(client, "u1")

    assert result["taste"] == "error"   # taste patladı
    assert result["recap"] == "ok"      # recap yine de çalıştı


def test_recap_hatalari_error_olur(monkeypatch):
    """refresh_user_recaps errors>0 dönerse recap 'error' işaretlenir (sözleşme §1.5)."""
    def fake_recap(client, user_id):
        return {"periods": 2, "written": 1, "errors": 1}
    monkeypatch.setattr(
        "app.pipeline.recap_runner.refresh_user_recaps", fake_recap, raising=False
    )

    client = _Client()
    result = mod.refresh_after_import(client, "u1")
    assert result["recap"] == "error"


def test_hicbir_hata_yukseltilmez(monkeypatch):
    """İkisi de patlasa bile fonksiyon dönmeli (export akışını bozmaz)."""
    def boom_recap(client, user_id):
        raise RuntimeError("recap zinciri çöktü")
    monkeypatch.setattr(
        "app.pipeline.recap_runner.refresh_user_recaps", boom_recap, raising=False
    )

    client = _Client(raise_on={"refresh_user_taste"})
    result = mod.refresh_after_import(client, "u1")  # exception fırlatmamalı

    assert result["taste"] == "error"
    assert result["recap"] == "error"

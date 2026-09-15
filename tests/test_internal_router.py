"""internal_router — anlık tazeleme endpoint testleri (FAZ 2).

Runner'lar monkeypatch'lenir (DB/HTTP'ye gitmez); test edilen: secret koruması,
kind yönlendirmesi, debounce.
"""
import importlib
import os

import pytest
from fastapi.testclient import TestClient


def _client(monkeypatch, secret=None):
    # ÖNEMLİ: import/reload zinciri app.config'i çeker ve load_dotenv(".env.local")
    # gerçek WORKER_SHARED_SECRET'ı os.environ'a GERİ koyar. Bu yüzden env'i
    # reload'dan SONRA ayarlamak zorunlu — yoksa dev secret'ı sızar.
    import app.routers.internal_router as mod
    importlib.reload(mod)
    if secret is None:
        monkeypatch.delenv("WORKER_SHARED_SECRET", raising=False)
    else:
        monkeypatch.setenv("WORKER_SHARED_SECRET", secret)
    # get_client'ı ve runner'ları sahteleştir
    monkeypatch.setattr(mod, "get_client", lambda: object())
    monkeypatch.setattr(mod, "_crypto_key", lambda: "key")
    monkeypatch.setattr(mod, "_recently_played_debounced", lambda c, u: False)
    monkeypatch.setattr(mod, "run_one_recently_played_sync",
                        lambda c, h, k, user_id: {"outcome": "success", "user_id": user_id})
    monkeypatch.setattr(mod, "run_one_playlist_refresh",
                        lambda c, h, k, user_id: {"outcome": "success", "user_id": user_id})
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(mod.router)
    return TestClient(app), mod


def test_refresh_both_calls_both_runners(monkeypatch):
    client, _ = _client(monkeypatch)
    r = client.post("/internal/refresh", json={"user_id": "u1", "kind": "both"})
    assert r.status_code == 200
    data = r.json()
    assert data["recently_played"]["outcome"] == "success"
    assert data["playlists"]["outcome"] == "success"


def test_refresh_recently_played_only(monkeypatch):
    client, _ = _client(monkeypatch)
    r = client.post("/internal/refresh", json={"user_id": "u1", "kind": "recently_played"})
    data = r.json()
    assert "recently_played" in data
    assert "playlists" not in data


def test_debounce_skips_recently_played(monkeypatch):
    client, mod = _client(monkeypatch)
    monkeypatch.setattr(mod, "_recently_played_debounced", lambda c, u: True)
    r = client.post("/internal/refresh", json={"user_id": "u1", "kind": "recently_played"})
    data = r.json()
    assert data["recently_played"] == {"skipped": True, "reason": "debounce"}


def test_secret_required_when_configured(monkeypatch):
    client, _ = _client(monkeypatch, secret="topsecret")
    # header yok → 401
    r = client.post("/internal/refresh", json={"user_id": "u1", "kind": "both"})
    assert r.status_code == 401
    # yanlış secret → 401
    r = client.post("/internal/refresh", json={"user_id": "u1", "kind": "both"},
                    headers={"X-Worker-Secret": "wrong"})
    assert r.status_code == 401
    # doğru secret → 200
    r = client.post("/internal/refresh", json={"user_id": "u1", "kind": "both"},
                    headers={"X-Worker-Secret": "topsecret"})
    assert r.status_code == 200


def test_no_secret_in_dev_allows_request(monkeypatch):
    client, _ = _client(monkeypatch, secret=None)
    r = client.post("/internal/refresh", json={"user_id": "u1", "kind": "both"})
    assert r.status_code == 200

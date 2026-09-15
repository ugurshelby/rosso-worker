"""taste_runner — refresh_user_taste RPC çağrısı + izolasyon testleri."""
from app.pipeline import taste_runner as runner


class _FakeClient:
    """play_events distinct user_id döner; rpc çağrılarını izler.

    fail_users: bu user_id'ler için rpc exception fırlatır (izolasyon testi).
    """
    def __init__(self, user_ids, fail_users=None):
        self._user_ids = user_ids
        self._fail = set(fail_users or [])
        self.rpc_calls: list[str] = []

    def table(self, name):
        assert name == "play_events"
        client = self
        class _Q:
            def select(self, *a, **k): return self
            def limit(self, *a, **k): return self
            def execute(self):
                class _R:
                    data = [{"user_id": u} for u in client._user_ids]
                return _R()
        return _Q()

    def rpc(self, fn, params):
        assert fn == "refresh_user_taste"
        uid = params["p_user_id"]
        client = self
        class _Exec:
            def execute(self):
                if uid in client._fail:
                    raise RuntimeError("boom")
                client.rpc_calls.append(uid)
                class _R:
                    data = None
                return _R()
        return _Exec()


def test_bos_kullanici_empty_doner():
    result = runner.run_taste_refresh(_FakeClient([]))
    assert result["outcome"] == "empty"
    assert result["users_processed"] == 0


def test_her_kullanici_icin_rpc_cagrilir():
    client = _FakeClient(["u1", "u2", "u3"])
    result = runner.run_taste_refresh(client)
    assert result["outcome"] == "success"
    assert result["users_processed"] == 3
    assert result["errors"] == 0
    assert set(client.rpc_calls) == {"u1", "u2", "u3"}


def test_distinct_user_id_tekillestirilir():
    # aynı user_id birden çok play_events satırında → tek RPC çağrısı
    client = _FakeClient(["u1", "u1", "u1", "u2"])
    result = runner.run_taste_refresh(client)
    assert result["users_processed"] == 2
    assert sorted(client.rpc_calls) == ["u1", "u2"]


def test_bir_kullanici_hatasi_digerlerini_durdurmaz():
    client = _FakeClient(["u1", "u2", "u3"], fail_users=["u2"])
    result = runner.run_taste_refresh(client)
    assert result["outcome"] == "partial"
    assert result["users_processed"] == 2  # u1, u3
    assert result["errors"] == 1
    assert "u2" not in client.rpc_calls


def test_hepsi_patlarsa_error():
    client = _FakeClient(["u1", "u2"], fail_users=["u1", "u2"])
    result = runner.run_taste_refresh(client)
    assert result["outcome"] == "error"
    assert result["users_processed"] == 0
    assert result["errors"] == 2

"""match_batch_runner — build_match_batch RPC çağrısı + izolasyon testleri."""
from app.pipeline import match_batch_runner as runner


class _FakeClient:
    """social_profiles distinct user_id döner; rpc çağrılarını izler.

    fail_users: bu user_id'ler için rpc exception fırlatır (izolasyon testi).
    """
    def __init__(self, user_ids, fail_users=None, fail_suggestion=False):
        self._user_ids = user_ids
        self._fail = set(fail_users or [])
        self._fail_suggestion = fail_suggestion
        self.rpc_calls: list[str] = []
        # FAZ 6: her RPC'nin çağrı SIRASINI izler.
        # ⚠ Sıra kritik: build_suggestion_batch, build_match_batch'ten SONRA
        #   koşmalı ("o günün eşleşme slotu hariç" kuralı).
        self.call_log: list[tuple[str, str]] = []

    def table(self, name):
        assert name == "social_profiles"
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
        # ⚠ 2026-08-10: eskiden burada `assert fn == "build_match_batch"`
        #   vardı. Runner'a yeni RPC'ler eklenince o assertion `try/except`
        #   içinde SESSİZCE YUTULDU — testler "geçiyor" görünürken öneri
        #   çağrısı hiç doğrulanmıyordu (ölçüldü: suggestion_errors=2).
        #   Artık üç RPC de tanınıyor ve sırası kayda geçiyor.
        assert fn in (
            "build_match_batch",
            "persist_discover_contrast",
            "build_suggestion_batch",
        ), f"beklenmeyen RPC: {fn}"
        uid = params["p_user_id"]
        client = self
        class _Exec:
            def execute(self):
                client.call_log.append((fn, uid))
                if fn == "build_match_batch":
                    if uid in client._fail:
                        raise RuntimeError("boom")
                    client.rpc_calls.append(uid)
                elif fn == "build_suggestion_batch" and client._fail_suggestion:
                    raise RuntimeError("oneri patladi")
                class _R:
                    data = 5
                return _R()
        return _Exec()


def test_bos_kullanici_empty_doner():
    result = runner.run_match_batch(_FakeClient([]))
    assert result["outcome"] == "empty"
    assert result["users_processed"] == 0


def test_her_kullanici_icin_rpc_cagrilir():
    client = _FakeClient(["u1", "u2", "u3"])
    result = runner.run_match_batch(client)
    assert result["outcome"] == "success"
    assert result["users_processed"] == 3
    assert result["errors"] == 0
    assert set(client.rpc_calls) == {"u1", "u2", "u3"}


def test_distinct_user_id_tekillestirilir():
    client = _FakeClient(["u1", "u1", "u1", "u2"])
    result = runner.run_match_batch(client)
    assert result["users_processed"] == 2
    assert sorted(client.rpc_calls) == ["u1", "u2"]


def test_bir_kullanici_hatasi_digerlerini_durdurmaz():
    client = _FakeClient(["u1", "u2", "u3"], fail_users=["u2"])
    result = runner.run_match_batch(client)
    assert result["outcome"] == "partial"
    assert result["users_processed"] == 2  # u1, u3
    assert result["errors"] == 1
    assert "u2" not in client.rpc_calls


def test_hepsi_patlarsa_error():
    client = _FakeClient(["u1", "u2"], fail_users=["u1", "u2"])
    result = runner.run_match_batch(client)
    assert result["outcome"] == "error"
    assert result["users_processed"] == 0
    assert result["errors"] == 2


# ═══════════════════════════════════════════════════════════════════
# FAZ EŞLEŞME-V2 · FAZ 6 — öneri paketi runner'a bağlandı
# ═══════════════════════════════════════════════════════════════════

def test_oneri_paketi_her_kullanici_icin_cagrilir():
    client = _FakeClient(["u1", "u2"])
    result = runner.run_match_batch(client)
    assert result["suggestion_written"] == 2
    assert result["suggestion_errors"] == 0


def test_oneri_ESLESMEDEN_SONRA_kosar():
    """🔴 Sıra bağlayıcı — ters olursa aynı profil iki yerde birden çıkar.

    `build_suggestion_batch` "o günün eşleşme slotundakiler hariç" diyor;
    eşleşme partisi yazılmadan koşarsa o filtre boş kümeye bakar.
    """
    client = _FakeClient(["u1"])
    runner.run_match_batch(client)

    sira = [fn for fn, uid in client.call_log if uid == "u1"]
    assert sira.index("build_match_batch") < sira.index("build_suggestion_batch"), (
        f"öneri eşleşmeden ÖNCE koştu: {sira}"
    )


def test_oneri_hatasi_eslesmeyi_BOZMAZ():
    """Öneri keşif zenginleştirmesi; eşleşme ana üründür — izole olmalı."""
    client = _FakeClient(["u1", "u2"], fail_suggestion=True)
    result = runner.run_match_batch(client)

    assert result["outcome"] == "success"      # eşleşme sağlam
    assert result["users_processed"] == 2
    assert result["errors"] == 0
    assert result["suggestion_errors"] == 2    # öneri düştü ama sessizce

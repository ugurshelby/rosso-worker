"""stats_pkg_runner — tazelik kuralı, boş-veri ayrımı ve izolasyon.

⚠ EN KRİTİK TEST: `test_veri_yoksa_hata_degil_empty`. RPC `false` döndüğünde
(kullanıcının dinleme verisi yok) bu bir HATA değildir — paket yazılmaz ve UI
"hazırlanıyor" gösterir. Yanlışlıkla hata sayılırsa cron `partial`/`error`
raporlar, biz de olmayan bir sorunu kovalarız.

⚠ İKİNCİ KRİTİK: `test_dogru_rpc_cagriliyor`. Bu runner `pattern_pkg_runner`
ile neredeyse birebir aynı — kopyala/yapıştır sırasında yanlış RPC adı kalırsa
cron ÇALIŞIR, `success` raporlar ve YANLIŞ paketi tazeler. Hiçbir şey görünür
biçimde kırılmaz.
"""
from datetime import datetime, timedelta, timezone

from app.pipeline import stats_pkg_runner as runner


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


class _FakeClient:
    """`recap_real_user_ids` aday listesi döner; paket tazeliğini taklit eder.

    pkg_ages: {user_id: saat} — paketin yaşı. Anahtar yoksa paket hiç yok.
    empty_users: bu kullanıcılarda RPC False döner (veri yok).
    fail_users: bu kullanıcılarda RPC patlar (izolasyon).
    """

    def __init__(self, user_ids, pkg_ages=None, empty_users=None, fail_users=None):
        self._user_ids = user_ids
        self._pkg_ages = pkg_ages or {}
        self._empty = set(empty_users or [])
        self._fail = set(fail_users or [])
        self.built: list[str] = []
        self.rpc_names: list[str] = []
        self.tables: list[str] = []

    def rpc(self, fn, params):
        client = self
        self.rpc_names.append(fn)
        if fn == "recap_real_user_ids":
            class _E:
                def execute(self):
                    class _R:
                        data = client._user_ids
                    return _R()
            return _E()

        assert fn == "build_user_stats_pkg", f"beklenmeyen RPC: {fn}"
        uid = params["p_user_id"]

        class _E:
            def execute(self):
                if uid in client._fail:
                    raise RuntimeError("boom")
                client.built.append(uid)
                class _R:
                    data = False if uid in client._empty else True
                return _R()
        return _E()

    def table(self, name):
        self.tables.append(name)
        assert name == "user_stats_pkg"
        client = self

        class _Q:
            def __init__(self):
                self._uid = None

            def select(self, *a, **k):
                return self

            def eq(self, col, val):
                if col == "user_id":
                    self._uid = val
                return self

            def gte(self, *a, **k):
                return self

            def limit(self, *a, **k):
                return self

            def execute(self):
                age = client._pkg_ages.get(self._uid)
                fresh = age is not None and age < runner.STALE_AFTER_HOURS

                class _R:
                    data = [{"generated_at": _iso(age or 0)}] if fresh else []
                return _R()

        return _Q()


def test_bos_kullanici_empty_doner():
    result = runner.run_stats_pkg(_FakeClient([]))
    assert result["outcome"] == "empty"


def test_paketi_olmayan_kullanici_uretilir():
    client = _FakeClient(["u1"])
    result = runner.run_stats_pkg(client)

    assert client.built == ["u1"]
    assert result["outcome"] == "success"
    assert result["users_processed"] == 1


def test_dogru_rpc_ve_tablo_kullaniliyor():
    """⚠ Bu runner pattern_pkg_runner'ın neredeyse birebir aynısı.

    Kopyala/yapıştır sırasında yanlış RPC/tablo adı kalırsa cron ÇALIŞIR,
    `success` raporlar ve YANLIŞ paketi tazeler — hiçbir şey görünür biçimde
    kırılmaz. Bu test o sessiz hatanın tek bekçisi.
    """
    client = _FakeClient(["u1"])
    runner.run_stats_pkg(client)

    assert "build_user_stats_pkg" in client.rpc_names
    assert "build_user_pattern_pkg" not in client.rpc_names
    assert "build_user_taste_pkg" not in client.rpc_names
    assert client.tables == ["user_stats_pkg"]


def test_taze_paket_atlanir():
    """20 saatlik eşiğin bekçisi — bozulursa cron her koşumda herkesi yeniden
    hesaplar ve bunu kimse fark etmez."""
    client = _FakeClient(["u1"], pkg_ages={"u1": 5})
    result = runner.run_stats_pkg(client)

    assert client.built == []
    assert result["skipped"] == 1
    assert result["outcome"] == "empty"


def test_bayat_paket_yeniden_uretilir():
    client = _FakeClient(["u1"], pkg_ages={"u1": 25})
    result = runner.run_stats_pkg(client)

    assert client.built == ["u1"]
    assert result["users_processed"] == 1


def test_esikte_duran_paket_taze_sayilir():
    """19 saat < 20 → henüz bayat değil."""
    client = _FakeClient(["u1"], pkg_ages={"u1": 19})
    result = runner.run_stats_pkg(client)
    assert client.built == []
    assert result["skipped"] == 1


def test_veri_yoksa_hata_degil_empty():
    """⚠ RPC False = 'kullanıcının verisi yok', HATA DEĞİL.

    Yanlışlıkla hata sayılırsa cron partial/error raporlar ve olmayan bir
    sorunu kovalarız.
    """
    client = _FakeClient(["u1"], empty_users=["u1"])
    result = runner.run_stats_pkg(client)

    assert result["errors"] == 0, "veri yoklugu HATA degil"
    assert result["empty"] == 1
    assert result["users_processed"] == 0
    assert result["outcome"] == "empty"


def test_karisik_veri_ve_bos_kullanici():
    client = _FakeClient(["u1", "u2"], empty_users=["u2"])
    result = runner.run_stats_pkg(client)

    assert result["users_processed"] == 1
    assert result["empty"] == 1
    assert result["errors"] == 0
    assert result["outcome"] == "success"


def test_force_tazelik_kontrolunu_atlar():
    client = _FakeClient(["u1", "u2"], pkg_ages={"u1": 1, "u2": 2})
    result = runner.run_stats_pkg(client, force=True)

    assert set(client.built) == {"u1", "u2"}
    assert result["skipped"] == 0


def test_bir_kullanicinin_hatasi_digerlerini_durdurmaz():
    client = _FakeClient(["u1", "u2", "u3"], fail_users=["u2"])
    result = runner.run_stats_pkg(client)

    assert set(client.built) == {"u1", "u3"}
    assert result["outcome"] == "partial"
    assert result["errors"] == 1


def test_hepsi_patlarsa_error():
    client = _FakeClient(["u1", "u2"], fail_users=["u1", "u2"])
    result = runner.run_stats_pkg(client)

    assert result["outcome"] == "error"
    assert result["errors"] == 2


def test_aday_listesi_test_profillerini_dislar():
    """`is_test` politikası: cron test profillerine dokunmaz."""
    client = _FakeClient(["u1"])
    runner.run_stats_pkg(client)

    assert "recap_real_user_ids" in client.rpc_names
    assert "recap_user_ids" not in client.rpc_names


def test_tazelik_kontrolu_patlarsa_uretilir():
    class _BrokenTable(_FakeClient):
        def table(self, name):
            class _Q:
                def select(self, *a, **k): return self
                def eq(self, *a, **k): return self
                def gte(self, *a, **k): return self
                def limit(self, *a, **k): return self
                def execute(self): raise RuntimeError("tablo yok")
            return _Q()

    client = _BrokenTable(["u1"])
    result = runner.run_stats_pkg(client)

    assert client.built == ["u1"]


def test_aday_sorgusu_patlarsa_error():
    class _BrokenRpc(_FakeClient):
        def rpc(self, fn, params):
            raise RuntimeError("aday listesi yok")

    result = runner.run_stats_pkg(_BrokenRpc(["u1"]))

    assert result["outcome"] == "error"
    assert result["users_processed"] == 0

"""journey_pkg_runner — tazelik kuralı, izolasyon ve is_test politikası.

⚠ EN KRİTİK TEST: `test_taze_paket_atlanir`. Efendim'in tazelik kararı
(*"aylık bile yeter"*) gece yükünü 30 kat düşürüyor — ama YALNIZ atlama
mantığı çalışırsa. Bozulursa hiçbir şey görünür biçimde kırılmaz; paketler
doğru üretilmeye devam eder, sadece cron her gece 30 kat fazla iş yapar ve
bunu kimse fark etmez. Bu yüzden testle çivileniyor.
"""
from datetime import datetime, timedelta, timezone

from app.pipeline import journey_pkg_runner as runner


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


class _FakeClient:
    """`recap_real_user_ids` aday listesi döner; paket tazeliğini taklit eder.

    pkg_ages: {user_id: gün} — açık yıl paketinin yaşı. Anahtar yoksa paket
    hiç yok demektir (ilk üretim).
    fail_users: bu kullanıcılarda `build_journey_year_pkg` patlar (izolasyon).
    """

    def __init__(self, user_ids, pkg_ages=None, fail_users=None):
        self._user_ids = user_ids
        self._pkg_ages = pkg_ages or {}
        self._fail = set(fail_users or [])
        self.built: list[str] = []

    def rpc(self, fn, params):
        client = self
        if fn == "recap_real_user_ids":
            class _E:
                def execute(self):
                    class _R:
                        data = client._user_ids
                    return _R()
            return _E()

        assert fn == "build_journey_year_pkg", f"beklenmeyen RPC: {fn}"
        uid = params["p_user_id"]

        class _E:
            def execute(self):
                if uid in client._fail:
                    raise RuntimeError("boom")
                client.built.append(uid)
                class _R:
                    data = 7  # yedi yıl yazıldı
                return _R()
        return _E()

    def table(self, name):
        assert name == "journey_year_pkg"
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
                # cutoff = 30 gün. Paket yoksa ya da eskiyse boş döner
                # (= "üretilmeli"); tazeyse bir satır döner (= "atla").
                fresh = age is not None and age < runner.STALE_AFTER_DAYS

                class _R:
                    data = [{"year": 2026, "generated_at": _iso(age or 0)}] if fresh else []
                return _R()

        return _Q()


def test_bos_kullanici_empty_doner():
    result = runner.run_journey_pkg(_FakeClient([]))
    assert result["outcome"] == "empty"
    assert result["users_processed"] == 0


def test_paketi_olmayan_kullanici_uretilir():
    client = _FakeClient(["u1"])  # pkg_ages boş → paket yok
    result = runner.run_journey_pkg(client)

    assert client.built == ["u1"]
    assert result["outcome"] == "success"
    assert result["years_written"] == 7


def test_taze_paket_atlanir():
    """⚠ Efendim'in 'aylık yeter' kararının bekçisi.

    Bozulursa hiçbir şey görünür biçimde kırılmaz — cron yalnız 30 kat fazla
    çalışır ve kimse fark etmez.
    """
    client = _FakeClient(["u1"], pkg_ages={"u1": 5})  # 5 günlük paket

    result = runner.run_journey_pkg(client)

    assert client.built == [], "taze paket YENIDEN uretilmemeli"
    assert result["skipped"] == 1
    assert result["users_processed"] == 0
    assert result["outcome"] == "empty"


def test_bayat_paket_yeniden_uretilir():
    client = _FakeClient(["u1"], pkg_ages={"u1": 31})  # 30 günü geçmiş

    result = runner.run_journey_pkg(client)

    assert client.built == ["u1"]
    assert result["users_processed"] == 1
    assert result["skipped"] == 0


def test_esikte_duran_paket_taze_sayilir():
    """29 gün < 30 → henüz bayat değil. Sınır kayarsa cron erken koşar."""
    client = _FakeClient(["u1"], pkg_ages={"u1": 29})
    result = runner.run_journey_pkg(client)
    assert client.built == []
    assert result["skipped"] == 1


def test_force_tazelik_kontrolunu_atlar():
    """Şema değişikliği sonrası toplu yeniden üretim yolu."""
    client = _FakeClient(["u1", "u2"], pkg_ages={"u1": 1, "u2": 2})

    result = runner.run_journey_pkg(client, force=True)

    assert set(client.built) == {"u1", "u2"}
    assert result["users_processed"] == 2
    assert result["skipped"] == 0


def test_bir_kullanicinin_hatasi_digerlerini_durdurmaz():
    client = _FakeClient(["u1", "u2", "u3"], fail_users=["u2"])

    result = runner.run_journey_pkg(client)

    assert set(client.built) == {"u1", "u3"}
    assert result["outcome"] == "partial"
    assert result["errors"] == 1
    assert result["users_processed"] == 2


def test_hepsi_patlarsa_error():
    client = _FakeClient(["u1", "u2"], fail_users=["u1", "u2"])

    result = runner.run_journey_pkg(client)

    assert result["outcome"] == "error"
    assert result["errors"] == 2


def test_aday_listesi_test_profillerini_dislar():
    """`is_test` politikası (Efendim): test profilleri paketini bir kez alır,
    cron onlara DOKUNMAZ. Bu yüzden aday RPC'si `recap_real_user_ids` olmalı —
    `recap_user_ids` (33 kullanıcı) DEĞİL."""
    calls: list[str] = []

    class _SpyClient(_FakeClient):
        def rpc(self, fn, params):
            calls.append(fn)
            return super().rpc(fn, params)

    runner.run_journey_pkg(_SpyClient(["u1"]))

    assert "recap_real_user_ids" in calls
    assert "recap_user_ids" not in calls, "test profilleri dislanmali (Efendim karari)"


def test_aday_sorgusu_patlarsa_error_doner():
    class _BrokenClient:
        def rpc(self, fn, params):
            class _E:
                def execute(self):
                    raise RuntimeError("RPC yok")
            return _E()

    result = runner.run_journey_pkg(_BrokenClient())
    assert result["outcome"] == "error"
    assert result["users_processed"] == 0


def test_tazelik_kontrolu_patlarsa_uretilir():
    """Kontrol edilemiyorsa bayat veri göstermektense fazladan hesapla."""
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
    result = runner.run_journey_pkg(client)

    assert client.built == ["u1"]
    assert result["users_processed"] == 1

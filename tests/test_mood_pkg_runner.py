"""mood_pkg_runner — çift izolasyon, tazelik kuralı, boş-veri ayrımı.

⚠ EN KRİTİK TEST: `test_bir_moodun_hatasi_digerlerini_dusurmez`. Bu runner
kullanıcı başına BEŞ çağrı yapıyor; bir mood patlarsa diğer dördü yazılmaya
devam etmeli. Aksi hâlde tek bir bozuk mood, kullanıcının TÜM an sayfalarını
"hazırlanıyor"da bırakır.

⚠ İKİNCİ KRİTİK: `test_dogru_rpc_ve_tablo_kullaniliyor`. Altı paket runner'ı
neredeyse birebir aynı; yanlış RPC adı kalırsa cron ÇALIŞIR, `success`
raporlar ve YANLIŞ paketi tazeler.
"""
from datetime import datetime, timedelta, timezone

from app.pipeline import mood_pkg_runner as runner


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


class _FakeClient:
    """`recap_real_user_ids` aday listesi döner; paket tazeliğini taklit eder.

    pkg_ages:    {user_id: saat} — paketin yaşı. Anahtar yoksa paket hiç yok.
    empty_moods: {(user_id, mood)} — RPC False döner (o mood için veri yok).
    fail_moods:  {(user_id, mood)} — RPC patlar (izolasyon).
    """

    def __init__(self, user_ids, pkg_ages=None, empty_moods=None, fail_moods=None):
        self._user_ids = user_ids
        self._pkg_ages = pkg_ages or {}
        self._empty = set(empty_moods or [])
        self._fail = set(fail_moods or [])
        self.built: list[tuple[str, str]] = []
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

        assert fn == "build_mood_pkg", f"beklenmeyen RPC: {fn}"
        uid = params["p_user_id"]
        mood = params["p_mood_key"]

        class _E:
            def execute(self):
                if (uid, mood) in client._fail:
                    raise RuntimeError("boom")
                client.built.append((uid, mood))
                class _R:
                    data = False if (uid, mood) in client._empty else True
                return _R()
        return _E()

    def table(self, name):
        self.tables.append(name)
        assert name == "mood_pkg"
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
    result = runner.run_mood_pkg(_FakeClient([]))
    assert result["outcome"] == "empty"


def test_kullanici_basina_BES_mood_yazilir():
    client = _FakeClient(["u1"])
    result = runner.run_mood_pkg(client)

    assert len(client.built) == len(runner.MOOD_KEYS), "kullanici basina 5 mood yazilmali"
    assert {m for _, m in client.built} == set(runner.MOOD_KEYS)
    assert result["users_processed"] == 1
    assert result["moods_written"] == len(runner.MOOD_KEYS)
    assert result["outcome"] == "success"


def test_dogru_rpc_ve_tablo_kullaniliyor():
    """⚠ Altı paket runner'ı neredeyse birebir aynı — yanlış ad sessiz hata."""
    client = _FakeClient(["u1"])
    runner.run_mood_pkg(client)

    assert "build_mood_pkg" in client.rpc_names
    assert "build_user_stats_pkg" not in client.rpc_names
    assert "build_user_period_pkg" not in client.rpc_names
    assert "build_user_pattern_pkg" not in client.rpc_names
    assert client.tables == ["mood_pkg"]


def test_bir_moodun_hatasi_digerlerini_dusurmez():
    """⚠ EN KRİTİK: tek bozuk mood, kullanıcının TÜM an sayfalarını
    "hazırlanıyor"da bırakmamalı."""
    client = _FakeClient(["u1"], fail_moods=[("u1", "odak")])
    result = runner.run_mood_pkg(client)

    yazilan = {m for _, m in client.built}
    assert yazilan == set(runner.MOOD_KEYS) - {"odak"}
    assert result["moods_written"] == len(runner.MOOD_KEYS) - 1
    assert result["errors"] == 1
    assert result["outcome"] == "partial"
    assert result["users_processed"] == 1, "diger moodlar yazildi, kullanici islendi sayilir"


def test_bir_kullanicinin_hatasi_digerlerini_durdurmaz():
    client = _FakeClient(
        ["u1", "u2"],
        fail_moods=[("u1", m) for m in runner.MOOD_KEYS],
    )
    result = runner.run_mood_pkg(client)

    assert {u for u, _ in client.built} == {"u2"}
    assert result["moods_written"] == len(runner.MOOD_KEYS)
    assert result["errors"] == len(runner.MOOD_KEYS)
    assert result["outcome"] == "partial"


def test_taze_paket_atlanir():
    """20 saatlik eşiğin bekçisi — bozulursa cron her koşumda 5 kat fazla
    çalışır ve kimse fark etmez."""
    client = _FakeClient(["u1"], pkg_ages={"u1": 5})
    result = runner.run_mood_pkg(client)

    assert client.built == []
    assert result["skipped"] == 1
    assert result["outcome"] == "empty"


def test_tazelik_kontrolu_kullanici_basina_TEK_sorgu():
    """Mood başına sorgu atmak 5 kat REST isteği demekti."""
    client = _FakeClient(["u1", "u2"], pkg_ages={"u1": 5, "u2": 5})
    runner.run_mood_pkg(client)

    assert client.tables == ["mood_pkg", "mood_pkg"], "kullanici basina 1 kontrol"


def test_bayat_paket_yeniden_uretilir():
    client = _FakeClient(["u1"], pkg_ages={"u1": 25})
    result = runner.run_mood_pkg(client)

    assert len(client.built) == len(runner.MOOD_KEYS)
    assert result["users_processed"] == 1


def test_esikte_duran_paket_taze_sayilir():
    client = _FakeClient(["u1"], pkg_ages={"u1": 19})
    result = runner.run_mood_pkg(client)
    assert client.built == []
    assert result["skipped"] == 1


def test_veri_yoksa_hata_degil_empty():
    """⚠ RPC False = 'o mood icin veri yok', HATA DEĞİL."""
    client = _FakeClient(["u1"], empty_moods=[("u1", m) for m in runner.MOOD_KEYS])
    result = runner.run_mood_pkg(client)

    assert result["errors"] == 0, "veri yoklugu HATA degil"
    assert result["empty"] == len(runner.MOOD_KEYS)
    assert result["moods_written"] == 0
    assert result["users_processed"] == 0
    assert result["outcome"] == "empty"


def test_karisik_dolu_ve_bos_mood():
    client = _FakeClient(["u1"], empty_moods=[("u1", "odak"), ("u1", "sabah")])
    result = runner.run_mood_pkg(client)

    assert result["moods_written"] == len(runner.MOOD_KEYS) - 2
    assert result["empty"] == 2
    assert result["errors"] == 0
    assert result["outcome"] == "success"


def test_force_tazelik_kontrolunu_atlar():
    client = _FakeClient(["u1"], pkg_ages={"u1": 1})
    result = runner.run_mood_pkg(client, force=True)

    assert len(client.built) == len(runner.MOOD_KEYS)
    assert result["skipped"] == 0


def test_hepsi_patlarsa_error():
    client = _FakeClient(
        ["u1"], fail_moods=[("u1", m) for m in runner.MOOD_KEYS]
    )
    result = runner.run_mood_pkg(client)

    assert result["outcome"] == "error"
    assert result["errors"] == len(runner.MOOD_KEYS)


def test_aday_listesi_test_profillerini_dislar():
    client = _FakeClient(["u1"])
    runner.run_mood_pkg(client)

    assert "recap_real_user_ids" in client.rpc_names
    assert "recap_user_ids" not in client.rpc_names


def test_mood_anahtarlari_kod_ile_ayni():
    """⚠ `MOOD_KEYS` `mood.ts`'teki MOODS ile aynı olmalı; ayrışırsa
    `build_mood_pkg` (0169, 0269, 0271) geçersiz anahtarı reddeder ve cron patlar.
    2026-08-11: su_siralar + dagittik_galiba eklendi (migration 0269).
    2026-08-11 gece oturumu: takinti + kesif + arsiv eklendi (migration 0271),
    katalog 10'a tamamlandı."""
    assert set(runner.MOOD_KEYS) == {
        "gece3", "gecesurus", "su_siralar", "gunduz", "sabah", "odak",
        "takinti", "dagittik_galiba", "kesif", "arsiv",
    }
    assert len(runner.MOOD_KEYS) == 10


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
    runner.run_mood_pkg(client)

    assert len(client.built) == len(runner.MOOD_KEYS)


def test_aday_sorgusu_patlarsa_error():
    class _BrokenRpc(_FakeClient):
        def rpc(self, fn, params):
            raise RuntimeError("aday listesi yok")

    result = runner.run_mood_pkg(_BrokenRpc(["u1"]))

    assert result["outcome"] == "error"
    assert result["users_processed"] == 0

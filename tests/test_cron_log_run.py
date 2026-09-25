"""`log_run()` — cron tur özetleri GEÇERLİ JSON olmalı.

Canlı kanıt (2026-08-01, worker logları): enrichment cron'u aylarca şunu yazdı:

    {"run":"enrichment","status":"empty",...,"backfill":{'outcome': 'empty', ...}}
                                                        ^^^^^^^^^^^^^^^^^^^^^^^^
Dıştaki JSON geçerli, İÇTEKİ değil. Sebep: `logger.info('...%s...', bir_dict)`
çağrısı `%s` ile dict'e `repr()` uyguluyor → tek tırnak. Aynı tuzağın ikinci
biçimi `quota_hit` alanındaydı: Python `True`/`False` yazıyordu, JSON
`true`/`false` bekler.

Göz okuduğu sürece zararsızdı; `system_logs` panelinde makineyle ayrıştırılınca
o satırlar patlardı — yani hata, en çok ihtiyaç duyulan anda ortaya çıkacaktı.

⚠ Bu testler `json.loads` ile GERÇEKTEN ayrıştırır. Sabit metin karşılaştırması
yapsalardı bozuk çıktıyı da "doğru" sayabilirlerdi.
"""
import json
import logging

import pytest

from app.cron._logging import log_run


@pytest.fixture
def kayitli(monkeypatch):
    """logger.info'ya giden ham metni yakalar."""
    yakalanan: list[str] = []
    logger = logging.getLogger("rosso.worker.test.log_run")
    monkeypatch.setattr(logger, "info", lambda msg, *a, **k: yakalanan.append(msg))
    return logger, yakalanan


def test_dict_alan_gecerli_json_uretir(kayitli):
    """⚠ ASIL HATA: dict `repr()` ile tek tırnaklı basılıyordu."""
    logger, yakalanan = kayitli

    log_run(
        logger, "enrichment",
        status="empty", processed=0, updated=0, skipped_deadline=0,
        backfill={"outcome": "empty", "artists": 0, "updated": 0, "no_match": 0},
    )

    ham = yakalanan[0]
    # Tek tırnak = repr sızıntısı. Bu satır bozuk sürümde KIRMIZI olur.
    assert "'" not in ham, f"repr() sizintisi (tek tirnak): {ham}"

    payload = json.loads(ham)  # bozuk sürümde burada patlar
    assert payload["run"] == "enrichment"
    assert payload["backfill"]["outcome"] == "empty"
    assert payload["backfill"]["artists"] == 0


def test_bool_alan_json_true_false_olur(kayitli):
    """⚠ İKİNCİ HATA: Python `True` yazılıyordu, JSON `true` bekler."""
    logger, yakalanan = kayitli

    log_run(logger, "cover_backfill", status="success", quota_hit=True)

    ham = yakalanan[0]
    assert "True" not in ham, f"Python bool sizintisi: {ham}"
    assert '"quota_hit": true' in ham or '"quota_hit":true' in ham

    payload = json.loads(ham)
    assert payload["quota_hit"] is True


def test_turkce_karakter_kacisla_bozulmaz(kayitli):
    """`ensure_ascii=False` — sanatçı/şarkı adları logda okunabilir kalmalı."""
    logger, yakalanan = kayitli

    log_run(logger, "test", artist="Aydın Kurtoğlu", title="Tüh tüh")

    ham = yakalanan[0]
    assert "Aydın Kurtoğlu" in ham  # ı... değil
    assert json.loads(ham)["artist"] == "Aydın Kurtoğlu"


def test_serilestirilemeyen_tip_turu_dusurmez(kayitli):
    """`default=str` — bir tur özeti ASLA logging yüzünden kaybolmamalı."""
    logger, yakalanan = kayitli

    class Garip:
        def __str__(self) -> str:
            return "garip-nesne"

    log_run(logger, "test", nesne=Garip())  # patlamamalı

    payload = json.loads(yakalanan[0])
    assert payload["nesne"] == "garip-nesne"


def test_run_alani_her_zaman_ilk_ve_var(kayitli):
    """`run` alanı log ayrıştırıcısının çapası — kaybolmamalı."""
    logger, yakalanan = kayitli

    log_run(logger, "mood_pkg", status="success")

    payload = json.loads(yakalanan[0])
    assert payload["run"] == "mood_pkg"
    assert list(payload.keys())[0] == "run"


def test_ic_ice_dict_ve_liste_bozulmaz(kayitli):
    """Derin yapılar da geçerli JSON olmalı (gelecekteki alanlar için)."""
    logger, yakalanan = kayitli

    log_run(logger, "test", detay={"liste": [1, 2, {"ic": True}], "bos": None})

    payload = json.loads(yakalanan[0])
    assert payload["detay"]["liste"][2]["ic"] is True
    assert payload["detay"]["bos"] is None

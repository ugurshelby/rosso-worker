"""ZIP yapı denetimi — kötü niyetli/bozuk arşivler worker'a ulaşmadan reddedilir."""
import io
import zipfile

import pytest

from app.services.zip_guard import MAX_ENTRIES, ZipGuardError, denetle_zip


def _zip(girdiler: dict[str, bytes], sikistirma=zipfile.ZIP_DEFLATED) -> zipfile.ZipFile:
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w", sikistirma) as zf:
        for ad, veri in girdiler.items():
            zf.writestr(ad, veri)
    tampon.seek(0)
    return zipfile.ZipFile(tampon)


def test_gercek_spotify_yapisi_gecer():
    zf = _zip({
        "Spotify Extended Streaming History/Streaming_History_Audio_2024_0.json": b"[]",
        "Spotify Extended Streaming History/ReadMeFirst_ExtendedStreamingHistory.pdf": b"%PDF",
    })
    denetle_zip(zf)  # fırlatmamalı


def test_yol_gecisi_reddedilir():
    with pytest.raises(ZipGuardError) as e:
        denetle_zip(_zip({"../../etc/cron.d/x.json": b"[]"}))
    assert e.value.kod == "yol_gecisi"


@pytest.mark.parametrize("ad", ["/etc/passwd.json", "C:/x.json", "a/../../b.json"])
def test_mutlak_ve_tuhaf_yollar_reddedilir(ad):
    with pytest.raises(ZipGuardError) as e:
        denetle_zip(_zip({ad: b"[]"}))
    assert e.value.kod == "yol_gecisi"


def test_ic_ice_arsiv_reddedilir():
    with pytest.raises(ZipGuardError) as e:
        denetle_zip(_zip({"a.json": b"[]", "icteki.zip": b"PK"}))
    assert e.value.kod == "ic_ice_arsiv"


def test_calistirilabilir_ve_betik_reddedilir():
    for ad in ("virus.exe", "run.sh", "x.js", "x.py", "noext"):
        with pytest.raises(ZipGuardError) as e:
            denetle_zip(_zip({"a.json": b"[]", ad: b"x"}))
        assert e.value.kod == "izinsiz_dosya_turu", ad


def test_json_yoksa_reddedilir():
    with pytest.raises(ZipGuardError) as e:
        denetle_zip(_zip({"notlar.txt": b"merhaba"}))
    assert e.value.kod == "json_yok"


def test_cok_fazla_giris_reddedilir():
    girdiler = {f"d/{i}.json": b"[]" for i in range(MAX_ENTRIES + 1)}
    with pytest.raises(ZipGuardError) as e:
        denetle_zip(_zip(girdiler))
    assert e.value.kod == "cok_fazla_giris"


def test_yinelenen_giris_reddedilir():
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w") as zf:
        zf.writestr("a.json", b"[]")
        with pytest.warns(UserWarning):
            zf.writestr("a.json", b"[1]")
    tampon.seek(0)
    with pytest.raises(ZipGuardError) as e:
        denetle_zip(zipfile.ZipFile(tampon))
    assert e.value.kod == "yinelenen_giris"


def test_sifreli_giris_reddedilir():
    zf = _zip({"a.json": b"[]"})
    zf.infolist()[0].flag_bits |= 0x1
    with pytest.raises(ZipGuardError) as e:
        denetle_zip(zf)
    assert e.value.kod == "sifreli_giris"


def test_egzotik_sikistirma_reddedilir():
    zf = _zip({"a.json": b"[]" * 10}, sikistirma=zipfile.ZIP_BZIP2)
    with pytest.raises(ZipGuardError) as e:
        denetle_zip(zf)
    assert e.value.kod == "desteklenmeyen_sikistirma"


def test_tek_giris_cok_buyukse_reddedilir(monkeypatch):
    zf = _zip({"a.json": b"[]"})
    zf.infolist()[0].file_size = 600 * 1024 * 1024
    with pytest.raises(ZipGuardError) as e:
        denetle_zip(zf)
    assert e.value.kod == "giris_cok_buyuk"


def test_supheli_oran_reddedilir():
    zf = _zip({"a.json": b"[]"})
    info = zf.infolist()[0]
    info.compress_size = 1024 * 1024
    info.file_size = 1024 * 1024 * 300  # 300x (300 MB < tek giriş sınırı 512 MB)
    with pytest.raises(ZipGuardError) as e:
        denetle_zip(zf)
    assert e.value.kod == "giris_orani_supheli"


def test_run_one_export_bozuk_zipi_reddeder_ve_dosyayi_siler():
    from app.pipeline.export_runner import run_one_export

    silinenler = []

    class _Bucket:
        def download(self, path):
            return b"bu bir zip degil"
        def remove(self, paths):
            silinenler.extend(paths)

    class _Storage:
        def from_(self, bucket):
            return _Bucket()

    class _Client:
        storage = _Storage()

    class _Ayar:
        export_bucket = "spotify-exports"

    sonuc = run_one_export(_Client(), _Ayar(), {"id": "j1", "user_id": "u1", "file_path": "u1/j1.zip"})
    assert sonuc["outcome"] == "error"
    assert sonuc["error"] == "gecersiz_zip"
    assert silinenler == ["u1/j1.zip"]

"""ZIP yapı ve içerik denetimi — kötü niyetli/bozuk yüklemelere karşı ilk savunma.

Kullanıcı yüklediği ZIP'i kendisi seçer; içeriği hiçbir zaman güvenilir değildir.
`zip_detect.guard_zip_bomb` yalnız toplam açılmış boyutu/oranı sınırlar. Bu
modül ARŞİVİN YAPISINI denetler — hiçbir girdiyi AÇMADAN, yalnız ZipInfo
metadata'sıyla (ucuz, güvenli):

  • giriş sayısı tavanı (yüz binlerce boş dosyayla bellek/CPU tüketme)
  • giriş başına açılmış boyut ve sıkıştırma oranı
  • yol geçişi: `..` bileşeni, mutlak yol, sürücü harfi, ters eğik çizgi, NUL
  • iç içe arşiv (zip-in-zip, gz, tar, 7z …)
  • şifreli giriş (parser açamaz; sessizce takılırdı)
  • yalnız stored/deflate sıkıştırma (egzotik yöntemler bomba riskini büyütür)
  • aynı adla birden çok giriş (parser'ı yanıltma / "zip confusion")
  • izinli uzantı listesi (yürütülebilir/betik reddedilir)
  • en az bir .json (Spotify export'u JSON'dur; içi JSON'suz arşiv değildir)

Gerçek Spotify export'u bunların çok altında kalır (ölçüm: dinleme geçmişi
~10 MB, ~10-40 dosya). Sınırlar bu ölçümün üstünde cömert pay bırakır.

Hiçbir dosya yolu diske YAZILMAZ (worker bellekten okur), yani yol geçişi
bugün doğrudan exploit değildir; yine de ileride biri `extractall` eklerse
tuzak kalmasın diye reddedilir (savunma derinliği).
"""
from __future__ import annotations

import posixpath
import zipfile

# ── Sınırlar ─────────────────────────────────────────────────────────────────
MAX_ENTRIES = 1000
MAX_ENTRY_UNCOMPRESSED = 512 * 1024 * 1024  # tek dosya 512 MB (toplam sınırı zip_detect'te 2 GB)
MAX_ENTRY_RATIO = 200                       # açılmış/sıkışmış; yalnız ≥1 MB sıkışmış girdilerde
_MIN_COMPRESSED_FOR_RATIO = 1024 * 1024
MAX_NAME_LENGTH = 512
MAX_COMPONENT_LENGTH = 255
#: Streaming event tavanı. ~125 bin event ≈ 10 MB; 2 M, ~16× pay.
MAX_STREAMING_EVENTS = 2_000_000

_ALLOWED_METHODS = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
_ALLOWED_EXTENSIONS = frozenset({".json", ".pdf", ".txt", ".md", ".csv", ".html", ".htm"})
_NESTED_ARCHIVE_EXTENSIONS = frozenset({
    ".zip", ".gz", ".tgz", ".tar", ".bz2", ".xz", ".7z", ".rar", ".jar", ".war", ".lz", ".zst",
})


class ZipGuardError(Exception):
    """ZIP yapısı güvenli değil. `kod` kısa makine kodu, mesaj ayrıntıdır."""

    def __init__(self, kod: str, ayrinti: str = "") -> None:
        super().__init__(f"{kod}: {ayrinti}" if ayrinti else kod)
        self.kod = kod
        self.ayrinti = ayrinti


def _uzanti(ad: str) -> str:
    taban = ad.rsplit("/", 1)[-1]
    if "." not in taban:
        return ""
    return "." + taban.rsplit(".", 1)[-1].lower()


def _yol_guvenli_mi(ad: str) -> bool:
    if "\x00" in ad or "\\" in ad:
        return False
    if ad.startswith("/") or (len(ad) >= 2 and ad[1] == ":"):
        return False
    normal = posixpath.normpath(ad)
    if normal == ".." or normal.startswith("../") or "/../" in f"/{normal}/":
        return False
    return all(len(parca) <= MAX_COMPONENT_LENGTH for parca in ad.split("/"))


def denetle_zip(zf: zipfile.ZipFile) -> None:
    """Arşiv yapısını doğrula; güvensizse `ZipGuardError` fırlat.

    Girdileri AÇMAZ. `zip_detect.guard_zip_bomb`'dan ÖNCE çağrılır — bu
    denetim daha ucuz ve daha spesifik hata kodu verir.
    """
    bilgiler = zf.infolist()
    if len(bilgiler) > MAX_ENTRIES:
        raise ZipGuardError("cok_fazla_giris", f"{len(bilgiler)} > {MAX_ENTRIES}")

    gorulen: set[str] = set()
    json_sayisi = 0

    for info in bilgiler:
        ad = info.filename
        if len(ad) > MAX_NAME_LENGTH:
            raise ZipGuardError("ad_cok_uzun", f"{len(ad)} karakter")
        if not _yol_guvenli_mi(ad):
            raise ZipGuardError("yol_gecisi", ad[:120])
        if ad in gorulen:
            raise ZipGuardError("yinelenen_giris", ad[:120])
        gorulen.add(ad)

        if info.is_dir():
            continue

        if info.flag_bits & 0x1:
            raise ZipGuardError("sifreli_giris", ad[:120])
        if info.compress_type not in _ALLOWED_METHODS:
            raise ZipGuardError("desteklenmeyen_sikistirma", f"{ad[:80]} yontem={info.compress_type}")

        uzanti = _uzanti(ad)
        if uzanti in _NESTED_ARCHIVE_EXTENSIONS:
            raise ZipGuardError("ic_ice_arsiv", ad[:120])
        if uzanti not in _ALLOWED_EXTENSIONS:
            raise ZipGuardError("izinsiz_dosya_turu", ad[:120])
        if uzanti == ".json":
            json_sayisi += 1

        if info.file_size > MAX_ENTRY_UNCOMPRESSED:
            raise ZipGuardError("giris_cok_buyuk", f"{ad[:80]} {info.file_size} bayt")
        if info.compress_size >= _MIN_COMPRESSED_FOR_RATIO:
            oran = info.file_size / max(info.compress_size, 1)
            if oran > MAX_ENTRY_RATIO:
                raise ZipGuardError("giris_orani_supheli", f"{ad[:80]} {oran:.0f}x")

    if json_sayisi == 0:
        raise ZipGuardError("json_yok", "arsivde hic .json dosyasi yok")

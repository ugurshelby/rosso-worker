"""token_cipher — TS `token-cipher.ts` ile bit-uyumu testleri.

Format: "iv_base64:tag_base64:data_base64" (3 parça). Daha önce tek-blob varsayan
yanlış bir implementasyon vardı; sessizce fallback yapıp şifreli veriyi olduğu gibi
döndürüyordu (canlı Spotify token'ıyla keşfedildi, 2026-07-02).

FAZ 5 (2026-07-11): iki dil artık tek KDF paylaşıyor — SHA-256(raw). Eskiden
TS `slice(0,32)` UTF-8, Python `encode()[:32]` kullanıyordu; bunlar yalnız anahtar
saf ASCII iken TESADÜFEN örtüşüyordu. Buradaki testler o tesadüfü bir SÖZLEŞMEYE
çevirir — biri KDF'yi bozarsa test yakalar.
"""
import base64
import hashlib

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.services.token_cipher import decrypt_token, encrypt_token

_TEST_KEY = "a" * 44  # gerçek anahtarımızla aynı biçim (44 char base64)


def _encrypt_with(plaintext: str, raw_key: bytes) -> str:
    """Verilen HAM 32 baytla TS formatında şifreler (iv:tag:data)."""
    iv = b"\x01" * 12  # deterministik
    blob = AESGCM(raw_key).encrypt(iv, plaintext.encode(), None)
    return ":".join([
        base64.b64encode(iv).decode(),
        base64.b64encode(blob[-16:]).decode(),
        base64.b64encode(blob[:-16]).decode(),
    ])


def _new_kdf(raw: str) -> bytes:
    return hashlib.sha256(raw.encode()).digest()


def _legacy_kdf(raw: str) -> bytes:
    return raw.encode()[:32].ljust(32, b"\x00")


# ── KDF sözleşmesi: TS ile birebir aynı olmak ZORUNDA ────────────────────────


def test_kdf_sha256_ile_turetilir():
    """KDF = SHA-256(raw). TS `createHash('sha256').update(raw,'utf8').digest()`
    ile matematiksel olarak AYNI. Bu satır değişirse iki dil ayrışır ve TÜM
    token'lar ölür — testin varlık sebebi budur."""
    from app.services.token_cipher import _derive
    raw = "herhangi-bir-anahtar-degeri-123456"
    assert _derive(raw) == hashlib.sha256(raw.encode()).digest()
    assert len(_derive(raw)) == 32


def test_kdf_cok_baytli_karakterde_de_32_bayt():
    """Eski KDF'nin gizli tuzağı: Türkçe karakterli anahtarda TS 32 KARAKTER
    (=37 bayt → AES patlar), Python 32 BAYT alıyordu. SHA-256 ile anahtarın
    alfabesi/uzunluğu artık ÖNEMSİZ — her girdi 32 bayta iner."""
    from app.services.token_cipher import _derive
    for raw in ["şifreÇOKgüçlü" + "x" * 31, "a" * 64, "a" * 32, "🎵" * 20]:
        assert len(_derive(raw)) == 32


def test_64_karakterlik_anahtar_artik_calisir():
    """ESKİDEN bu bir TUZAKTI: `openssl rand -hex 32` (64 char) → Python fromhex,
    TS UTF-8 slice → farklı bayt → tüm token'lar sessizce ölürdü. O yüzden 64-char
    açıkça REDDEDİLİYORDU. SHA-256 ile tuzak KÖKTEN kalktı: artık çalışmalı."""
    key64 = "f" * 64
    assert decrypt_token(encrypt_token("tok", key64), key64) == "tok"


# ── Çift-okuma geçişi: mevcut token'lar ölmemeli ─────────────────────────────


def test_legacy_ile_sifrelenmis_token_yeni_kodla_cozulur():
    """GEÇİŞİN KALBİ. Üretimdeki token'lar eski KDF ile şifrelendi. Yeni kod onları
    çözemezse tüm kullanıcılar platform bağlantısını kaybeder. Çift-okuma bunu önler."""
    legacy_ct = _encrypt_with("eski-token", _legacy_kdf(_TEST_KEY))
    assert decrypt_token(legacy_ct, _TEST_KEY) == "eski-token"


def test_yeni_kdf_ile_sifrelenmis_token_cozulur():
    new_ct = _encrypt_with("yeni-token", _new_kdf(_TEST_KEY))
    assert decrypt_token(new_ct, _TEST_KEY) == "yeni-token"


def test_encrypt_HER_ZAMAN_yeni_kdf_ile_yazar():
    """Yazma asla legacy'ye düşmemeli — yoksa göç hiç ilerlemez ve legacy dalı
    sonsuza kadar silemeyiz."""
    ct = encrypt_token("x", _TEST_KEY)
    iv, tag, data = (base64.b64decode(p) for p in ct.split(":"))
    # Yeni KDF ile çözülebilmeli
    assert AESGCM(_new_kdf(_TEST_KEY)).decrypt(iv, data + tag, None) == b"x"
    # Legacy KDF ile ÇÖZÜLEMEMELİ (yani gerçekten yeni anahtarla yazılmış)
    with pytest.raises(Exception):
        AESGCM(_legacy_kdf(_TEST_KEY)).decrypt(iv, data + tag, None)


def test_roundtrip():
    assert decrypt_token(encrypt_token("gizli", _TEST_KEY), _TEST_KEY) == "gizli"


# ── Sessiz-başarısızlık savunması (Fable 5 #1) ───────────────────────────────


def test_anahtar_varken_cozulemeyen_ciphertext_RAISE_eder():
    """Sessiz 401'in kök nedeni: çözülemeyen blob'u düz token sanıp
    `Bearer <blob>` göndermek. Artık patlar — hem yeni hem legacy KDF başarısızsa."""
    fake = ":".join([
        base64.b64encode(b"\x00" * 12).decode(),
        base64.b64encode(b"\x00" * 16).decode(),
        base64.b64encode(b"garbage-data").decode(),
    ])
    with pytest.raises(Exception):
        decrypt_token(fake, _TEST_KEY)


def test_3_parcali_olmayan_girdi_oldugu_gibi_doner():
    """Şifrelenmemiş düz metin (eski kayıtlar) — format tanınmıyor, dokunma."""
    assert decrypt_token("not-a-ciphertext", _TEST_KEY) == "not-a-ciphertext"


def test_anahtar_yoksa_ciphertext_oldugu_gibi_doner(monkeypatch):
    """dev/test: anahtar hiç yok → çözme denenmez."""
    monkeypatch.delenv("TOKEN_ENCRYPTION_KEY", raising=False)
    ct = _encrypt_with("plain", _new_kdf(_TEST_KEY))
    assert decrypt_token(ct, "") == ct

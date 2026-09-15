"""
AES-256-GCM token cipher — TS `src/lib/crypto/token-cipher.ts` ile BİT BİT AYNI.
Format: "iv_base64:tag_base64:data_base64" (3 parça, ':' ile ayrılmış).

İki taraf da hem OKUR hem YAZAR (TS OAuth callback'te şifreler, Python token
yenilerken yeniden şifreler) → anahtar türetmedeki en küçük sapma çift yönlü
veri kaybıdır.

── Anahtar türetme (FAZ 5, 2026-07-11) ──
Yeni: SHA-256(raw) → 32 bayt. TS `createHash('sha256')` ile matematiksel olarak
aynı sonuç. Anahtarın uzunluğu/alfabesi artık ÖNEMSİZ — 64-hex tuzağı kökten yok.

Eski (legacy): TS `raw.slice(0,32)` UTF-8 · Python `raw.encode()[:32]` + zero-pad.
Bunlar yalnız anahtar saf ASCII iken TESADÜFEN örtüşüyordu. Türkçe/çok baytlı bir
karakter girseydi TS 32 KARAKTER (=37 bayt → AES patlar), Python 32 BAYT alırdı.
Ortak kural yoktu, şans vardı.

── Geçiş (çift-okuma) ──
Yazma HER ZAMAN yeni KDF ile. Okuma önce yeni, olmazsa legacy ile denenir.
Mevcut token'lar kesintisiz çözülür ve her yenilenmede kendiliğinden göç eder.
Legacy dal, tüm token'lar göç ettikten sonra silinecek.
"""
import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _derive(raw: str) -> bytes:
    """Yeni KDF — TS `createHash('sha256').update(raw,'utf8').digest()` ile aynı."""
    return hashlib.sha256(raw.encode()).digest()


def _derive_legacy(raw: str) -> bytes:
    """Eski KDF — YALNIZ çözmede, yeni başarısız olursa. Asla şifrelemede kullanma."""
    return raw.encode()[:32].ljust(32, b"\x00")


def _raw_key(key_override: str = "") -> str:
    return key_override or os.environ.get("TOKEN_ENCRYPTION_KEY", "")


def decrypt_token(ciphertext: str, key_override: str = "") -> str:
    """TS ile şifrelenmiş token'ı çözer.

    Anahtar YOKSA (dev/test) ciphertext'i olduğu gibi döndürür. Anahtar VARSA ve
    çözme başarısızsa SESSİZCE ciphertext döndürmez — RAISE eder. Eski davranış
    çözülemeyen blob'u düz token sanıp `Bearer <blob>` gönderiyor, upstream 401
    sessizce yutuluyordu (Fable 5 #1).
    """
    raw = _raw_key(key_override)
    if not raw:
        return ciphertext  # dev/test: anahtar yok, olduğu gibi geç

    parts = ciphertext.split(":")
    if len(parts) != 3:
        # 3-parçalı format değil → zaten düz metin (şifrelenmemiş) kabul et
        return ciphertext
    iv = base64.b64decode(parts[0])
    tag = base64.b64decode(parts[1])
    data = base64.b64decode(parts[2])

    # cryptography lib AESGCM.decrypt tag'in ciphertext sonuna eklenmesini bekler.
    try:
        return AESGCM(_derive(raw)).decrypt(iv, data + tag, None).decode()
    except Exception:  # noqa: BLE001
        # Geçiş dönemi: eski KDF ile şifrelenmiş olabilir. Başarılı olursa çağıran
        # zaten yeni KDF ile yeniden yazacak (göç). Legacy de patlarsa hata YUTULMAZ.
        return AESGCM(_derive_legacy(raw)).decrypt(iv, data + tag, None).decode()


def encrypt_token(plaintext: str, key_override: str = "") -> str:
    """Token'ı şifreler (worker-tarafı token yenileme). TS `encrypt()` ile aynı.

    HER ZAMAN yeni KDF ile yazar — legacy asla yazmaz. Göç bu sayede ilerler.
    """
    raw = _raw_key(key_override)
    if not raw:
        return plaintext

    iv = os.urandom(12)
    # AESGCM.encrypt tag'i çıktının SONUNA ekler; TS formatı ise tag'i ayrı parça
    # olarak ortada bekler → ayırıp sıralıyoruz.
    blob = AESGCM(_derive(raw)).encrypt(iv, plaintext.encode(), None)
    data, tag = blob[:-16], blob[-16:]
    return ":".join([
        base64.b64encode(iv).decode(),
        base64.b64encode(tag).decode(),
        base64.b64encode(data).decode(),
    ])

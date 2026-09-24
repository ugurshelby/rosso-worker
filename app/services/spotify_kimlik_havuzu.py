"""Katalog bakım işleri için Spotify kimlik GRUPLARI — kullanıcı kotasıyla çalışma.

SORUN (2026-09-23, 1000 kullanıcı ölçeği): kapak/sanatçı görseli/şarkı bilgisi
dolguları tek bir Spotify app'in (Efendim'in paylaşılan app'i) kotasını
kullanıyordu. Kullanıcı sayısı arttıkça o tek kota tükenir, 429 cezası HERKES
için bakım işlerini durdururdu ve Efendim'in kendi hesabı da etkilenirdi.

ÇÖZÜM: her bakım turu kimlik GRUPLARI üzerinde çalışır. Grup = bir Spotify
app'i + o app'i kullanan kullanıcılar:

  • BYOC grubu    : kullanıcının KENDİ dev app'i. Yalnız o kullanıcının
                    dinlediği/paketlerindeki eksikler o app'in kotasından
                    doldurulur. 429 cezası yalnız o app'i durdurur
                    (cooldown sağlayıcısı 'spotify@<client_id>').
  • Paylaşılan   : Efendim'in app'i. YALNIZ paylaşılan app'le bağlı
                    kullanıcılar (oauth_client_id NULL ya da env id'si) için
                    çalışır; başkalarının işini üstlenmez.

Spotify bağlantısı OLMAYAN kullanıcılar (yalnız ZIP yükleyenler) hiçbir grupta
değildir: onların eksik kapakları Spotify'dan doldurulmaz (kimsenin kotası
onlar için harcanmaz).

`platform_connections.oauth_client_id` (migration 0341) hangi app'in
kullanıldığını söyler; şifreli sırlar `spotify_byoc_credentials`'tadır.
Kural web `resolveSpotifyCredentialsForConnection` ile aynı.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from app.services.token_cipher import decrypt_token

logger = logging.getLogger("rosso.worker.spotify_kimlik_havuzu")

PAYLASILAN_SAGLAYICI = "spotify"


@dataclass
class KimlikGrubu:
    """Tek bir Spotify app'i ve o app'in kotasıyla çalışacak kullanıcılar."""

    client_id: str
    client_secret: str
    user_ids: list[str] = field(default_factory=list)
    #: Cooldown/kota anahtarı. Paylaşılan: 'spotify'; BYOC: 'spotify@<client_id>'.
    saglayici: str = PAYLASILAN_SAGLAYICI

    @property
    def paylasilan_mi(self) -> bool:
        return self.saglayici == PAYLASILAN_SAGLAYICI


def kimlik_gruplari(client: Any, crypto_key: str) -> list[KimlikGrubu]:
    """Bakım turu için kimlik gruplarını kur. Boş gruplar dahil edilmez.

    Sıra: BYOC grupları (her biri kendi kotasında, birbirinden bağımsız) sonra
    paylaşılan grup. Sırlar çözülemeyen/doğrulanmamış BYOC kayıtları atlanır
    (o kullanıcının işi paylaşılana DÜŞMEZ — kota başkasının olmamalı).
    """
    shared_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
    shared_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "")

    try:
        baglantilar = (
            client.table("platform_connections")
            .select("user_id, oauth_client_id")
            .eq("platform", "spotify")
            .eq("is_active", True)
            .execute()
            .data
            or []
        )
    except Exception:  # noqa: BLE001
        logger.warning("platform_connections okunamadı — bakım turu kimlik grubu kuramadı")
        return []

    byoc_kullanicilar: dict[str, list[str]] = {}  # client_id -> [user_id]
    paylasilan_kullanicilar: list[str] = []
    for b in baglantilar:
        uid = b.get("user_id")
        kurucu = b.get("oauth_client_id")
        if not uid:
            continue
        if kurucu is None or kurucu == shared_id:
            paylasilan_kullanicilar.append(uid)
        else:
            byoc_kullanicilar.setdefault(kurucu, []).append(uid)

    gruplar: list[KimlikGrubu] = []

    if byoc_kullanicilar:
        try:
            satirlar = (
                client.table("spotify_byoc_credentials")
                .select("user_id, client_id, client_secret, verified_at")
                .execute()
                .data
                or []
            )
        except Exception:  # noqa: BLE001
            logger.warning("spotify_byoc_credentials okunamadı — BYOC grupları atlandı")
            satirlar = []

        sirlar: dict[str, str] = {}
        for satir in satirlar:
            cid = satir.get("client_id")
            if not cid or not satir.get("verified_at") or cid in sirlar:
                continue
            try:
                secret = decrypt_token(satir["client_secret"], crypto_key)
            except Exception:  # noqa: BLE001
                secret = None
            if secret:
                sirlar[cid] = secret

        for cid, uids in byoc_kullanicilar.items():
            if cid in sirlar:
                gruplar.append(
                    KimlikGrubu(
                        client_id=cid,
                        client_secret=sirlar[cid],
                        user_ids=uids,
                        saglayici=f"spotify@{cid}",
                    )
                )
            else:
                logger.warning("BYOC kimliği çözülemedi/doğrulanmamış: client_id=%s… — atlandı", cid[:6])

    if paylasilan_kullanicilar and shared_id and shared_secret:
        gruplar.append(
            KimlikGrubu(
                client_id=shared_id,
                client_secret=shared_secret,
                user_ids=paylasilan_kullanicilar,
                saglayici=PAYLASILAN_SAGLAYICI,
            )
        )

    return gruplar

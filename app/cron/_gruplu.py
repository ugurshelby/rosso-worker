"""Katalog bakım cron'ları için ortak: kimlik GRUPLARI üzerinde sırayla çalış.

Her grup kendi Spotify app'inin kotasıyla çalışır (bkz.
`app.services.spotify_kimlik_havuzu`). Toplam zaman bütçesi gruplara paylaştırılır:
BYOC grupları önce (küçük, hızlı biter), paylaşılan grup sonra. Bir grubun 429'u
yalnız o grubu durdurur; sıradaki grup etkilenmez.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

from app.services.spotify_kimlik_havuzu import KimlikGrubu, kimlik_gruplari

logger = logging.getLogger("rosso.worker.cron.gruplu")

#: Bir gruba ayrılabilecek en az bütçe (sn). Altındaysa grup bu tura kalmaz.
_MIN_GRUP_BUTCESI_S = 5.0


def gruplu_calistir(
    client: Any,
    settings: Any,
    calistir: Callable[[KimlikGrubu, float], dict[str, Any]],
    *,
    toplam_butce_s: float,
) -> dict[str, Any]:
    """`calistir(grup, kalan_butce_s)` her grup için çağrılır; sonuçlar toplanır.

    Dönüş: {outcome, processed, updated, skipped, quota_hit, groups, blocked_groups}.
    outcome: hiç grup yoksa 'empty'; hiçbiri iş yapmadıysa ve hepsi bloklu ise
    'blocked'; aksi halde en az bir grupta güncelleme varsa 'success', yoksa
    'empty'; herhangi bir grup yarıda kaldıysa 'partial'.
    """
    baslangic = time.monotonic()
    gruplar = kimlik_gruplari(client, getattr(settings, "token_encryption_key", ""))
    if not gruplar:
        return {
            "outcome": "empty", "processed": 0, "updated": 0, "skipped": 0,
            "quota_hit": False, "groups": 0, "blocked_groups": 0,
        }

    toplam = {"processed": 0, "updated": 0, "skipped": 0}
    quota_hit = False
    partial = False
    blocked = 0
    calisan = 0

    for grup in gruplar:
        kalan = toplam_butce_s - (time.monotonic() - baslangic)
        if kalan < _MIN_GRUP_BUTCESI_S:
            partial = True
            logger.info("Bütçe doldu — kalan gruplar sonraki tura: %s", grup.saglayici)
            break
        try:
            sonuc = calistir(grup, kalan)
        except Exception:  # noqa: BLE001
            # Bir grubun hatası (ör. o app'in token'ı reddedildi) diğerlerini durdurmaz.
            logger.exception("Kimlik grubu başarısız: %s", grup.saglayici)
            partial = True
            continue

        calisan += 1
        for k in toplam:
            toplam[k] += int(sonuc.get(k, 0) or 0)
        if sonuc.get("outcome") == "blocked":
            blocked += 1
        if sonuc.get("quota_hit") or sonuc.get("outcome") == "partial":
            partial = True
        quota_hit = quota_hit or bool(sonuc.get("quota_hit"))

    if calisan > 0 and blocked == calisan and toplam["processed"] == 0:
        outcome = "blocked"
    elif partial:
        outcome = "partial"
    else:
        outcome = "success" if toplam["updated"] else "empty"

    return {
        "outcome": outcome, **toplam, "quota_hit": quota_hit,
        "groups": len(gruplar), "blocked_groups": blocked,
    }

"""ZIP-sonrası anında tazeleme — yeni kullanıcı yüklemesini beklemez.

Bağlam (Efendim 2026-07-28, Yankı 3. organik kullanıcı): Yankı kayıt oldu,
Spotify bağladı, ZIP yükledi → 77K dinleme olayı yazıldı. Ama recap+taste
YALNIZ günlük cron'la üretiliyordu → Yankı kayıt saatinden sonra ilk turu
beklemek zorundaydı (dashboard yarım: dinleme sayıları var, taste kimliği +
recap YOK). Kötü ilk izlenim.

Çözüm: bir ZIP başarıyla işlenince o kullanıcının recap+taste'ini ANINDA
tetikle. Günlük cron (taste_refresh, recap_refresh) YEDEK katman olarak kalır
(herkesi tazeler, drift'i toplar). §1.7: bu ince tetikleyici cron'un üstüne
eklenir, onu bozmaz.

⚠ İzole: tazeleme HATASI export'u BOZMAZ. Export zaten başarıyla bitmiş, veri
DB'de; tazeleme başarısızsa gece cron nasıl olsa toparlar. Bu yüzden her hata
yutulur + loglanır, asla yükseltilmez.

Tek-kullanıcı fonksiyonları zaten var:
  - taste:  refresh_user_taste(p_user_id) RPC  (taste_runner ile aynı)
  - recap:  refresh_user_recaps(client, user_id)  (recap_runner tek-kullanıcı)
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("rosso.worker.post_import_refresh")


def refresh_after_import(client: Any, user_id: str) -> dict[str, Any]:
    """Bir ZIP işlendikten sonra tek kullanıcının recap+taste'ini tazeler.

    {taste, recap} döner; her biri 'ok' | 'error' | 'skipped'. Hiçbir hata
    yükseltilmez — export akışı bundan etkilenmez (gece cron yedek).
    """
    result = {"taste": "skipped", "recap": "skipped"}

    # 1) Taste — refresh_user_taste RPC (weights→behavioral→genre→identity zinciri).
    try:
        client.rpc("refresh_user_taste", {"p_user_id": user_id}).execute()
        result["taste"] = "ok"
    except Exception as exc:  # noqa: BLE001
        result["taste"] = "error"
        logger.warning("ZIP-sonrası taste tazeleme başarısız user=%s: %s", user_id, str(exc)[:200])

    # 2) Recap — tek-kullanıcı orkestratörü. recap_runner'ı burada import ederiz
    #    (döngüsel import riskini önler; export akışı recap_runner'a bağımlı olmasın).
    try:
        from app.pipeline.recap_runner import refresh_user_recaps

        # §1.5 (sözleşme okundu): refresh_user_recaps
        # {periods, written, errors, skipped_current} döner — 'outcome' YOK.
        # errors>0 ise kısmi başarısızlık; yine de yazılanlar korunur.
        recap_res = refresh_user_recaps(client, user_id)
        result["recap"] = "error" if recap_res.get("errors", 0) > 0 else "ok"
        result["recaps_written"] = recap_res.get("written", 0)
    except Exception as exc:  # noqa: BLE001
        result["recap"] = "error"
        logger.warning("ZIP-sonrası recap tazeleme başarısız user=%s: %s", user_id, str(exc)[:200])

    logger.info(
        '{"run":"post_import_refresh","user":"%s","taste":"%s","recap":"%s"}',
        user_id, result["taste"], result["recap"],
    )
    return result

"""Mood (an) listeleri paketi cron'u (Aşama 3 · paket #6).

Çalıştırma: python -m app.cron.mood_pkg  ·  GÜNLÜK

`mood_pkg` tablosunu doldurur: sabit anlar için 50'şer şarkı.
`/mood/[key]` detay sayfasını besler.

⚠ Diğer paket cron'larıyla KARIŞTIRMA — altısı ayrı iş:
    taste_pkg   → user_taste_pkg:   tür dağılımı + kronotip        (/taste)
    pattern_pkg → user_pattern_pkg: saatlik dağılım + platform     (/dashboard)
    stats_pkg   → user_stats_pkg:   StatBar sayımları + tür haritası
    period_pkg  → user_period_pkg:  Dinlemelerim top listeleri     (/dashboard)
    mood_pkg    → mood_pkg:         an listeleri                  (/mood/[key])
    journey_pkg → journey_year_pkg: yıl kartları + kapaklar        (/journey)

⚠ Bu cron kullanıcı başına ON paket yazar (0269: su_siralar + dagittik_galiba;
0271: takinti + kesif + arsiv eklendi) — Aşama 3'ün en pahalı gece yükü.
10.000 kullanıcıda kuyruk/batch gerekliliği ölçekte burada başlar.

Runner tazelik kontrolü yapıyor (20 saat), yani günlük tetiklemek güvenli:
aynı gün ikinci kez koşarsa hiçbir kullanıcı yeniden hesaplanmaz.

🔴 SIRA KRİTİK (0270, Efendim: haftalık senkron): `run_mood_weekly_sync`
mood_pkg'DEN SONRA çağrılır — senkron, TAZE pakete göre Spotify'ı günceller.
Ters sırada koşarsa dünün paketi Spotify'a yazılırdı.
⚠ İZOLE: senkron hatası mood_pkg turunu BOZMAZ. Mood paketleri ana ürün;
senkron ikincil bir kolaylık.
"""
from __future__ import annotations

import logging
import sys

import httpx

from app.cron._logging import setup_logging

setup_logging()
logger = logging.getLogger("rosso.worker.cron.mood_pkg")


def main() -> int:
    from app.config import get_settings
    from app.db import get_client
    from app.pipeline.mood_pkg_runner import run_mood_pkg
    from app.pipeline.mood_weekly_sync_runner import run_mood_weekly_sync
    from app.services import run_log

    client = get_client()
    settings = get_settings()
    crypto_key = getattr(settings, "token_encryption_key", "") or ""

    try:
        result = run_mood_pkg(client)
        logger.info(
            '{"run":"mood_pkg","status":"%s","users":%d,"moods":%d,"skipped":%d,'
            '"empty":%d,"errors":%d}',
            result["outcome"],
            result.get("users_processed", 0),
            result.get("moods_written", 0),
            result.get("skipped", 0),
            result.get("empty", 0),
            result.get("errors", 0),
        )

        sync_result: dict[str, object] = {}
        try:
            with httpx.Client(timeout=15) as http:
                sync_result = run_mood_weekly_sync(client, crypto_key, http)
            logger.info(
                '{"run":"mood_weekly_sync","status":"%s","synced":%d,"skipped":%d,"errors":%d}',
                sync_result.get("outcome"),
                sync_result.get("synced", 0),
                sync_result.get("skipped", 0),
                sync_result.get("errors", 0),
            )
        except Exception:  # noqa: BLE001
            logger.exception("mood_weekly_sync başarısız — mood_pkg turu etkilenmedi")

        combined = {**result, "weekly_sync": sync_result}
        run_log.record_run(client, "mood_pkg", outcome=result["outcome"], stats=combined)
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("Mood paket cron'u başarısız")
        run_log.record_run(client, "mood_pkg", outcome="error", error=str(exc)[:500])
        return 1


if __name__ == "__main__":
    sys.exit(main())

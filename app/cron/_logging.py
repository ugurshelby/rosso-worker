"""Cron entry'leri için ortak JSON logging kurulumu (Railway stdout)."""
from __future__ import annotations

import json
import logging
import sys


_STANDARD_KEYS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys())


class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # logger.error(..., extra={...}) ile eklenen alanları da yaz — daha önce
        # bunlar sessizce yutuluyordu (bkz. ytmusic_official._log_failure).
        for key, value in record.__dict__.items():
            if key not in _STANDARD_KEYS and key not in payload:
                payload[key] = value
        if record.exc_info:
            import traceback as tb
            payload["exc"] = tb.format_exception(*record.exc_info)[-1].strip()
        return json.dumps(payload, ensure_ascii=False, default=str)


def log_run(logger: logging.Logger, run: str, **fields: object) -> None:
    """Cron tur özetini GEÇERLİ JSON olarak yazar.

    ⚠ Neden var: cron'lar tur özetini `logger.info('{"run":"x","v":%s}', deger)`
    diye ELLE kuruyordu. `%s` Python nesnesine `repr()` uygular; dict tek
    tırnakla, bool `True`/`False` diye basılır — ikisi de GEÇERSİZ JSON.
    Canlı kanıt (2026-08-01): enrichment cron'u aylarca
    `"backfill":{'outcome': 'empty', ...}` yazdı. Göz okuduğu için kimse fark
    etmedi; makineyle ayrıştırılınca (system_logs paneli) o satır patlardı.

    Elle kurulan JSON'da her yeni alan bu hatayı tekrar davet eder — burada
    `json.dumps` tek yerde, bir kez doğru yapar.

    ⚠ Çıktı ŞEKLİ eski çağrılarla birebir aynı: mesajın kendisi bir JSON
    string'idir (dış formatter onu `msg` alanına koyar). Bilinçli: Efendim
    Railway loglarını bu şekilde izliyor, görünüm değişmemeli. `extra=`
    desenine geçiş ayrı ve daha büyük bir iş.

    `default=str`: datetime/Decimal gibi tipler patlamak yerine metne düşer —
    bir tur özeti ASLA logging yüzünden kaybolmamalı.
    """
    logger.info(json.dumps({"run": run, **fields}, ensure_ascii=False, default=str))


def setup_logging() -> None:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(_JSONFormatter())
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(h)
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

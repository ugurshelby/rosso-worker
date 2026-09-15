"""Worker→worker / Next→worker HTTP çağrıları için paylaşılan gizli anahtar header'ı.

Next.js'in korumalı iç uçları (internal_router: /internal/refresh) X-Worker-Secret
ile korunuyor. Bu helper aynı secret'ı header olarak üretir. Secret tanımlı
değilse (yerel dev) boş dict döner ve endpoint tarafındaki kontrol de atlanır.
"""
from __future__ import annotations

import os


def worker_secret_headers() -> dict[str, str]:
    """WORKER_SHARED_SECRET tanımlıysa X-Worker-Secret header'ı döner, yoksa {}."""
    secret = os.environ.get("WORKER_SHARED_SECRET", "")
    return {"X-Worker-Secret": secret} if secret else {}

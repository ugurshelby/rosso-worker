"""Worker→worker / Next→worker HTTP çağrıları için paylaşılan gizli anahtar header'ı.

Next.js'in korumalı iç uçları (internal_router: /internal/refresh) X-Worker-Secret
ile korunuyor. Bu helper aynı secret'ı header olarak üretir. Secret tanımlı
değilse (yerel dev) boş dict döner ve endpoint tarafındaki kontrol de atlanır.

⚠ Güvenlik notu (2026-09 audit): `WORKER_SHARED_SECRET` tanımsızken kontrolün
sessizce atlanması SADECE yerel geliştirmede kabul edilebilir. Prod ortamda
env var eksikse (deploy hatası, yanlış panel) `/internal/refresh` tamamen
kimliksiz açık kalırdı — gerçek Spotify sync + DB yazımı tetikleyen bir uç.
Bu yüzden "yerel dev" artık BELİRSİZLİKTEN değil, `WORKER_LOCAL_DEV=1` gibi
AÇIK bir opt-in'den anlaşılır (bkz. `is_local_dev_mode`). Opt-in yoksa ve
secret de yoksa → fail-closed (health.py router'ı hiç mount etmez / bu
fonksiyon 503 döner)."""
from __future__ import annotations

import os


def worker_secret_headers() -> dict[str, str]:
    """WORKER_SHARED_SECRET tanımlıysa X-Worker-Secret header'ı döner, yoksa {}."""
    secret = os.environ.get("WORKER_SHARED_SECRET", "")
    return {"X-Worker-Secret": secret} if secret else {}


def is_local_dev_mode() -> bool:
    """Açık yerel-dev opt-in'i: `WORKER_LOCAL_DEV=1` (veya true/yes).

    Codebase'de daha önce dev/prod ayrımını yapan bir `ENV`/`ENVIRONMENT`
    değişkeni YOKTU — "secret boşsa dev'dir" varsayımı buydu ve tam da bu
    audit'in bulduğu fail-open açığıydı. Artık dev modu VARSAYILAN değil,
    açıkça işaretlenmesi gereken bir durum.
    """
    return os.environ.get("WORKER_LOCAL_DEV", "").strip().lower() in ("1", "true", "yes")


def worker_secret_configured() -> bool:
    """`WORKER_SHARED_SECRET` tanımlı ve boş değil."""
    return bool(os.environ.get("WORKER_SHARED_SECRET", "").strip())

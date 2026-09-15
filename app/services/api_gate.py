"""Merkezî API geçidi — Spotify'a giden HER istek buradan geçer.

Efendim (2026-08-05): *"Hiç ceza yemeyeceğiz çünkü sistem her zaman hiyerarşiye
uygun, limitten haberdar ve limit sınırına uygun çalışacak."*

═══ NEDEN BU MODÜL — CANLI DENEYLE ÖLÇÜLDÜ (2026-08-05) ═══

Spotify kalan kotayı **hiç söylemiyor** (tüm yanıt başlıkları döküldü:
`X-RateLimit-Remaining` YOK). Ceza yiyince yalnız `Retry-After` geliyor.
Yani kalan bütçeyi bilmenin tek yolu KENDİ SAYACIMIZ.

Deney sonuçları:
  ① HIZ SINIR DEĞİL — 20 istek/sn'de bile 200 istek ceza almadı.
     Koddaki 250ms geçit (4 istek/sn) yanlış sorunu çözüyormuş.
  ② SINIR HACİM — 4 istek/sn sabit hızda **398 istekte** 429 geldi.
  ③ CEZA AĞIR — 85.898 sn = **23,86 SAAT**. Bir kez çarpınca gün bitiyor.
  ④ CEZA UÇ BAZLI — `tracks`/`artists`/`albums` 429 iken `search` 200 verdi.
  ⑤ CEZA SABİT BİTİŞE SAYAR — 429 alırken istek atmak süreyi uzatmıyor.

⚠ Bu modülün varlık sebebi ②+③: ceza yemek 1 GÜN kaybettiriyor. O yüzden
"429 gelince dur" YETMEZ — **429'a hiç varmamak** gerekir.

═══ İKİ AYRI KAPI ═══

    cooldown  → "ceza var mı?"        (api_cooldowns, 429 SONRASI)
    bütçe     → "hakkımız kaldı mı?"  (api_budgets,   429 ÖNCESİ)  ← asıl koruma

Ceza yokken de bütçe bitmiş olabilir. Amaç zaten bu.

═══ HİYERARŞİ ═══

Bütçe azaldıkça yalnız üst kattaki işler geçer. Kullanıcının EKRANDA gördüğü
şey, arka plan bakımından önceliklidir.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from app.services import cooldown

logger = logging.getLogger("rosso.worker.api_gate")

# ── Uç grupları (ölçüm ④: ceza uç bazlı) ────────────────────────────────────
# Tek "spotify" bayrağı yanlış model: `search` cezalıyken `tracks` çalışabilir
# (ya da tersi). Bütçe de ayrı tutulur.
SCOPE_CATALOG = "spotify:catalog"  # /v1/tracks, /v1/artists, /v1/albums
SCOPE_SEARCH = "spotify:search"    # /v1/search
SCOPE_USER = "spotify:user"        # kullanıcı token'lı: /me/*, /playlists/*

# ── Öncelik katları ─────────────────────────────────────────────────────────
# Sayı KÜÇÜLDÜKÇE öncelik artar. Bütçenin ne kadarı kaldığında bir katın
# çalışmayı bırakacağını `_MIN_REMAINING_RATIO` belirler.
PRIORITY_CRITICAL = 1  # kullanıcının ekranda beklediği (paket içeriği)
PRIORITY_HIGH = 2      # son dinlenenler — dashboard'un ilk gördüğü yer
PRIORITY_NORMAL = 3    # playlist tazeleme, otomatik liste
PRIORITY_LOW = 4       # arka plan bakımı, dolgu işleri

#: Bir kat, bütçenin bu oranından AZI kaldığında durur.
#: Örn. LOW: bütçenin %40'ı tükendiğinde artık çalışmaz — kalan %60'ı
#: üst katlara saklar. CRITICAL hep çalışır (0.0).
_MIN_REMAINING_RATIO = {
    PRIORITY_CRITICAL: 0.00,
    PRIORITY_HIGH: 0.10,
    PRIORITY_NORMAL: 0.25,
    PRIORITY_LOW: 0.60,
}

# ── Hız geçidi ──────────────────────────────────────────────────────────────
# ⚠ Ölçüm ① hızın sınır OLMADIĞINI gösterdi — ama geçidi KALDIRMIYORUZ:
# platform davranışı değişebilir ve 250ms bize hiçbir şeye mal olmuyor
# (3 kullanıcı için günde ~100 istek). Ucuz sigorta.
_PACE_GAP_S = 0.25
_pace_lock = threading.Lock()
_last_request_at = 0.0


class BudgetExhausted(Exception):
    """Günlük bütçe bitti — istek ATILMADI (429 yenmedi, önlendi)."""

    def __init__(self, scope: str, used: int, budget: int) -> None:
        self.scope = scope
        self.used = used
        self.budget = budget
        super().__init__(f"{scope} bütçesi doldu: {used}/{budget}")


class ProviderBlocked(Exception):
    """Sağlayıcı ceza altında — istek ATILMADI."""

    def __init__(self, provider: str, remaining_s: int) -> None:
        self.provider = provider
        self.remaining_s = remaining_s
        super().__init__(f"{provider} cezalı, {remaining_s}s kaldı")


@dataclass
class GateDecision:
    """Geçidin kararı. `allowed=False` ise İSTEK ATILMAZ."""

    allowed: bool
    reason: str          # 'ok' | 'blocked' | 'budget' | 'priority'
    remaining: int = 0
    used: int = 0
    budget: int = 0
    blocked_seconds: int = 0


def _provider_of(scope: str) -> str:
    """'spotify:catalog' → 'spotify' (cooldown sağlayıcı adı)."""
    return scope.split(":", 1)[0]


def check(
    client: Any,
    scope: str,
    *,
    count: int = 1,
    priority: int = PRIORITY_NORMAL,
) -> GateDecision:
    """İstek atmadan ÖNCE sor: geçebilir miyim?

    Üç kapıdan geçer:
      1. Cooldown — ceza var mı? (varsa hiç sorma, çık)
      2. Öncelik  — bu kat için yeterli bütçe kaldı mı?
      3. Bütçe    — hak var mı? (varsa ATOMİK olarak düşülür)

    ⚠ `allowed=True` dönerse bütçe ZATEN TÜKETİLMİŞTİR. İstek atılmazsa
    `refund()` çağrılmalı — yoksa sayaç gerçekten fazla gösterir.

    Bütçe ayrılmadan önce öncelik kontrolü yapılır: alt katlar bütçenin
    tamamını yiyip üst katları aç bırakamaz.
    """
    provider = _provider_of(scope)

    # ── Kapı 1: ceza ──
    blocked, remaining_s = cooldown.is_blocked(client, provider)
    if blocked:
        logger.info(
            "api_gate: %s cezalı (%ds kaldı) — istek atılmadı", provider, remaining_s
        )
        return GateDecision(
            allowed=False, reason="blocked", blocked_seconds=remaining_s
        )

    # ── Kapı 2+3: bütçe (öncelik eşiğiyle) ──
    try:
        res = client.rpc(
            "budget_check_and_consume", {"p_scope": scope, "p_count": count}
        ).execute()
        rows = res.data or []
    except Exception:  # noqa: BLE001
        # ⚠ Sayaç okunamıyorsa İSTEK ATMA. "Bilmiyorsam serbest" demek,
        # ölçülen 23,86 saatlik cezaya davetiye çıkarmaktır. Kapalı taraf güvenli.
        logger.warning("api_gate: bütçe sorgusu başarısız (%s) — istek atılmadı", scope)
        return GateDecision(allowed=False, reason="budget")

    if not rows:
        logger.warning("api_gate: %s bütçe satırı YOK — istek atılmadı", scope)
        return GateDecision(allowed=False, reason="budget")

    row = rows[0]
    allowed = bool(row.get("allowed"))
    remaining = int(row.get("remaining") or 0)
    used = int(row.get("used") or 0)
    budget = int(row.get("budget") or 0)

    if not allowed:
        logger.info(
            "api_gate: %s bütçesi doldu (%d/%d) — istek atılmadı", scope, used, budget
        )
        return GateDecision(
            allowed=False, reason="budget", remaining=remaining, used=used, budget=budget
        )

    # Öncelik eşiği: bütçe düşükken alt katlar durur.
    # (Bütçe TÜKETİLDİKTEN sonra bakılır; geçemezse iade edilir.)
    min_ratio = _MIN_REMAINING_RATIO.get(priority, 0.25)
    if budget > 0 and (remaining / budget) < min_ratio:
        refund(client, scope, count)
        logger.info(
            "api_gate: %s öncelik %d için bütçe düşük (kalan %d/%d, eşik %%%d) — "
            "üst katlara saklandı",
            scope, priority, remaining, budget, int(min_ratio * 100),
        )
        return GateDecision(
            allowed=False, reason="priority", remaining=remaining, used=used, budget=budget
        )

    return GateDecision(
        allowed=True, reason="ok", remaining=remaining, used=used, budget=budget
    )


def refund(client: Any, scope: str, count: int = 1) -> None:
    """Ayrılan bütçeyi geri ver — istek ATILMADIYSA çağrılır.

    Örn. geçit izin verdi ama ağ hatası yüzünden istek hiç gitmedi. İade
    edilmezse sayaç gerçeğin üstünde kalır ve gün boyu haksız yere kısıtlarız.
    """
    try:
        client.rpc("budget_check_and_consume", {"p_scope": scope, "p_count": -count}).execute()
    except Exception:  # noqa: BLE001
        # İade başarısızsa sayaç fazla gösterir — pencere dolunca düzelir.
        logger.debug("api_gate: bütçe iadesi başarısız (%s)", scope)


def paced_get(http: Any, url: str, **kwargs: Any) -> Any:
    """250ms geçitli GET. Ölçüm ① hızın sınır olmadığını gösterdi ama
    geçit ucuz bir sigorta olarak korunuyor (bkz. modül başlığı)."""
    global _last_request_at
    with _pace_lock:
        wait = _last_request_at + _PACE_GAP_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()
    return http.get(url, **kwargs)


def record_429(client: Any, scope: str, retry_after_s: float | None, reason: str) -> int:
    """429 yendi — cezayı DB'ye yaz VE bütçeyi tüketilmiş say.

    ⚠ Bütçe de doldurulur: 429 aldıysak gerçek kotayı zaten aşmışız demektir.
    Sayacı olduğu yerde bırakmak, ceza bitince aynı duvara koşmak olurdu
    (ölçülen desen: `pipeline_runs`'ta cover_backfill 06:04'te 81 istekte
    çarpmış, 13:04 ve 20:02'de 0 istekte — kota zaten doluydu).

    Süre bilinmiyorsa 1 saat varsayılır (kural-veritabani-islemleri.md §1).
    """
    provider = _provider_of(scope)
    used_s = cooldown.set_cooldown(
        client, provider, retry_after_s or 3600.0, reason=reason
    )

    try:
        # Bütçeyi tavana çek: bu pencerede daha fazla denemeyelim.
        client.rpc(
            "budget_check_and_consume", {"p_scope": scope, "p_count": 10_000}
        ).execute()
    except Exception:  # noqa: BLE001
        logger.debug("api_gate: 429 sonrası bütçe doldurulamadı (%s)", scope)

    logger.warning(
        "api_gate: %s 429 aldı — ceza %ds, bütçe kapatıldı (sebep=%s)",
        scope, used_s, reason,
    )
    return used_s


def status(client: Any) -> list[dict[str, Any]]:
    """Tüm kapsamların bütçe durumu (log/panel için — sayacı ARTIRMAZ)."""
    try:
        res = client.rpc("budget_status", {}).execute()
        return res.data or []
    except Exception:  # noqa: BLE001
        logger.warning("api_gate: budget_status okunamadı")
        return []

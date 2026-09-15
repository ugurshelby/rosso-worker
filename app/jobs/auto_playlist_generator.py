"""
Monthly auto-playlist generator job (plan §7.2).

Triggered by pgmq 'auto_playlist_queue' message (pg_cron fires 1st of month 01:00 UTC).

Critical rules:
- playlist_month = previous month (cron runs on 1 Jun → "May 2026 — Top 50")
- Every platform gets a NEW list each month — never sync to existing list (apple-critical §3)
- Apple append-only is irrelevant here: fresh new playlist each month, no removals
- YT: throttle + 429 backoff (no formula-based quota)
- One platform failure must not stop other platforms (isolation)
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import httpx
from supabase import create_client

from app.playlist_rules.rules import top_month, top_year, RuleTrack

logger = logging.getLogger(__name__)

# Varsayılan playlist kapakları — TÜM kullanıcılar için ortak (Efendim 2026-07-25).
# worker/assets/playlist-covers/monthly/{01..12}.jpg + annual.jpg.
# Kapaklar ≤256KB olmalı (Spotify limiti); repo'ya eklenirken küçültülür.
_COVERS_DIR = Path(__file__).resolve().parent.parent.parent / "assets" / "playlist-covers"


def _cover_path_for(rule_type: str, month: int) -> Optional[Path]:
    """Kural tipine göre varsayılan kapak dosyası. Yoksa None (kapak atlanır)."""
    if rule_type == "top_year":
        p = _COVERS_DIR / "annual.jpg"
    else:
        p = _COVERS_DIR / "monthly" / f"{month:02d}.jpg"
    return p if p.is_file() else None

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
WORKER_URL = os.environ.get("WORKER_URL", "http://localhost:8000")
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")

MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _previous_month() -> tuple[int, int]:
    """Return (year, month) for the month BEFORE now. Cron runs on 1 Jun → May."""
    now = datetime.now(timezone.utc)
    first_of_this_month = now.replace(day=1)
    last_month = first_of_this_month - timedelta(days=1)
    return last_month.year, last_month.month


def _month_playlist_name(year: int, month: int) -> str:
    """Monthly playlist name — 'August - 2026' (month token English)."""
    return f"{MONTH_NAMES[month]} - {year}"


def _year_playlist_name(year: int) -> str:
    """Yıllık playlist adı — 'Mayıs - 2026' kararıyla tutarlı: yalnız yıl → '2025'."""
    return str(year)


def _db():
    return create_client(SUPABASE_URL, SUPABASE_KEY)


# ── Token helpers ──────────────────────────────────────────────────────────────

def _get_platform_token(user_id: str, platform: str) -> dict:
    db = _db()
    resp = (
        db.table("platform_connections")
        .select("access_token, music_user_token, refresh_token, is_active, subscription_active")
        .eq("user_id", user_id)
        .eq("platform", platform)
        .single()
        .execute()
    )
    return resp.data or {}


def _decrypt_token(ciphertext: str) -> str:
    """Decrypt AES-256-GCM token (mirrors TS token-cipher.ts)."""
    from app.services.token_cipher import decrypt_token
    return decrypt_token(ciphertext)


def _get_spotify_access_token(user_id: str) -> Optional[str]:
    """Geçerli Spotify token'ını döner.
    
    Önce get_valid_spotify_token ile taze token almayı dener (otomatik refresh).
    Test mock'ları veya DB yoksa _get_platform_token + _decrypt_token fallback'ine düşer.
    """
    db = _db()
    if db is not None:
        try:
            from app.config import get_settings
            from app.services.spotify_token import get_valid_spotify_token
            settings = get_settings()
            crypto_key = getattr(settings, "token_encryption_key", "") or os.environ.get("TOKEN_ENCRYPTION_KEY", "")
            with httpx.Client(timeout=10) as sync_http:
                tok = get_valid_spotify_token(db, user_id, crypto_key, sync_http)
                if tok:
                    return tok
        except Exception as exc:  # noqa: BLE001
            logger.debug("get_valid_spotify_token fallback: %s", exc)

    tokens = _get_platform_token(user_id, "spotify")
    if not tokens.get("is_active") or not tokens.get("access_token"):
        return None
    return _decrypt_token(tokens["access_token"])


# ── Platform playlist creators ────────────────────────────────────────────────

async def _create_spotify_playlist(
    user_id: str,
    name: str,
    track_ids: list[str],
    cover_path: Optional[Path] = None,
) -> Optional[str]:
    """Create Spotify playlist, add tracks, follow (kütüphane), kapak yükle.

    Returns playlist_id or None.
    """
    from app.services import api_gate

    _db_client = _db()
    gate = api_gate.check(
        _db_client, api_gate.SCOPE_USER, count=5, priority=api_gate.PRIORITY_NORMAL
    )
    if not gate.allowed:
        logger.warning(
            "Otomatik playlist: geçit kapalı (%s) — oluşturma ertelendi, user=%s",
            gate.reason, user_id,
        )
        return None

    access_token = _get_spotify_access_token(user_id)
    if not access_token:
        logger.warning("Spotify valid token alinamadi (user %s)", user_id)
        api_gate.refund(_db_client, api_gate.SCOPE_USER, 5)  # istek atılmadı
        return None

    async with httpx.AsyncClient() as client:
        # /me çağrısı bağlantıyı erken doğrular (403/401 burada yakalanır) —
        # allowlist dışı kullanıcıda create'e boşuna gitmeyiz.
        me_resp = await client.get(
            "https://api.spotify.com/v1/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if me_resp.status_code != 200:
            logger.error("Spotify /me failed: %s", me_resp.status_code)
            return None

        # B13 (2026-07-10): Spotify Şub-2026'da POST /users/{id}/playlists ucunu
        # KALDIRDI (canlı test → 403). Yeni uç: POST /me/playlists. TS tarafı
        # (engine.ts) 2026-07-09'da geçmişti; Python unutulmuştu.
        # ⚠ ORTAK NOT: Spotify uçları iki dilde AYRI yaşıyor. Uç değişince
        # HEM engine.ts HEM burayı güncelle. Aynı hatayı ikinci kez yeme.
        create_resp = await client.post(
            "https://api.spotify.com/v1/me/playlists",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={"name": name, "public": False, "description": "Auto-generated by Rosso"},
        )
        if create_resp.status_code not in (200, 201):
            logger.error("Spotify create playlist failed: %s", create_resp.status_code)
            return None
        playlist_id = create_resp.json()["id"]

        # Add tracks in batches of 100 (Spotify limit)
        for i in range(0, len(track_ids), 100):
            batch = track_ids[i : i + 100]
            uris = [f"spotify:track:{tid}" for tid in batch]
            await client.post(
                f"https://api.spotify.com/v1/playlists/{playlist_id}/items",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json={"uris": uris},
            )

        # ⚠ FOLLOW ÇAĞRISI KALDIRILDI (2026-08-05). İki ayrı sebeple ölüydü:
        #   1. `PUT /playlists/{id}/followers` Şubat 2026'da KALDIRILDI → 403
        #   2. Gereksiz: `POST /me/playlists` listeyi zaten kütüphaneye ekliyor
        #      (canlıda ölçüldü — oluşturur oluşturmaz `/me/library/contains`
        #      `[true]` dönüyor ve liste `/me/playlists`'te görünüyor).
        # Halefi `PUT /me/library` ile denendi: zaten üye olana tekrar üyelik
        # **500** veriyor. Doğru davranış hiç çağırmamak.

        # KAPAK — varsayılan kapak (ikincil, ugc-image-upload scope ister).
        if cover_path is not None:
            try:
                b64 = base64.b64encode(cover_path.read_bytes()).decode("ascii")
                img_resp = await client.put(
                    f"https://api.spotify.com/v1/playlists/{playlist_id}/images",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "image/jpeg",
                    },
                    content=b64,
                )
                if img_resp.status_code not in (200, 202):
                    logger.warning(
                        "Spotify kapak yüklenemedi (%s) playlist %s — scope eksik olabilir",
                        img_resp.status_code, playlist_id,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Spotify kapak exception %s: %s", playlist_id, exc)

    logger.info("Spotify playlist created: %s (%d tracks)", playlist_id, len(track_ids))
    return playlist_id


# ── Track ID resolution ────────────────────────────────────────────────────────

def _resolve_track_ids(tracks: list[RuleTrack], platform: str) -> list[str]:
    """
    Look up platform-specific IDs for resolved tracks from the tracks table.
    Returns list of platform IDs (may be shorter than input if not all matched).
    """
    if not tracks:
        return []
    db = _db()
    track_ids = [t.track_id for t in tracks if t.track_id]
    if not track_ids:
        return []

    id_col = {
        "spotify": "spotify_id",
    }.get(platform)
    if not id_col:
        return []

    resp = (
        db.table("tracks")
        .select(f"id, {id_col}")
        .in_("id", track_ids)
        .execute()
    )
    rows = resp.data or []
    result = [row[id_col] for row in rows if row.get(id_col)]
    return result


# ── Platform push (bir kural için ortak) ────────────────────────────────────────

async def _push_rule_to_platforms(
    db,
    *,
    rule_id: str,
    user_id: str,
    playlist_name: str,
    tracks: list[RuleTrack],
    target_platforms: list[str],
    cover_path: Optional[Path] = None,
) -> int:
    """Bir kuralın track listesini her platforma bağımsız yazar. Başarılı platform
    sayısını döner. Bir platformun hatası diğerlerini durdurmaz (izolasyon).

    `cover_path`: varsayılan playlist kapağı (yalnız Spotify'da yükleniyor;
    Apple/YT kapak yükleme ayrı iş, şimdilik kapsam dışı)."""
    ok = 0
    for platform in target_platforms:
        platform_ids = _resolve_track_ids(tracks, platform)
        playlist_id: Optional[str] = None
        status = "failed"
        error_msg: Optional[str] = None

        try:
            if platform == "spotify":
                playlist_id = await _create_spotify_playlist(
                    user_id, playlist_name, platform_ids, cover_path=cover_path
                )

            if playlist_id:
                status = "completed" if len(platform_ids) == len(tracks) else "partial"
                ok += 1
            else:
                status = "failed"
        except Exception as exc:
            logger.error("Platform %s failed for rule %s: %s", platform, rule_id, exc)
            error_msg = str(exc)
            status = "failed"

        db.table("auto_playlist_runs").insert({
            "rule_id": rule_id,
            "generated_playlist_id": playlist_id,
            "platform": platform,
            "status": status,
            "track_count": len(platform_ids) if playlist_id else 0,
            "error_message": error_msg,
        }).execute()

    return ok


# ── Dönem dedup (başarılı üretim) ─────────────────────────────────────────────

def _period_start(now: datetime, rule_type: str) -> datetime:
    """Bu kural tipi için 'zaten üretildi' penceresinin başlangıcı."""
    if rule_type == "top_year":
        return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    # top_month: takvim ayı başı — Eylül'de Ağustos listesi, ay boyunca tek üretim
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _already_produced_this_period(db, rule_id: str, rule_type: str, now: datetime) -> bool:
    """Bu dönemde başarılı (completed/partial) üretim varsa tekrar oluşturma.

    Başarısız denemeler engellemez — gece cron'u ertesi gece yeniden dener.
    """
    period_start = _period_start(now, rule_type)
    resp = (
        db.table("auto_playlist_runs")
        .select("id")
        .eq("rule_id", rule_id)
        .in_("status", ["completed", "partial"])
        .gte("ran_at", period_start.isoformat())
        .limit(1)
        .execute()
    )
    return bool(resp.data)


# ── Main job ───────────────────────────────────────────────────────────────────

async def run_auto_playlist_job() -> dict:
    """
    Process all active auto_playlist_rules (opt-in: enabled=true) for all users.

    - top_month: her ay çalışır → bir önceki ayın top listesi ("Mayıs - 2026")
    - top_year:  bu takvim yılı içinde HENÜZ çalışmadıysa çalışır → bir önceki
      yılın top listesi ("2025"). Eskiden yalnız Ocak'ta çalışırdı — yıl
      ortasında kuralı açan kullanıcı Ocak'a dek üretim görmüyordu (Efendim
      2026-08-12: "top_year yalnız Ocak" kısıtlaması kaldırıldı). `last_run_at`
      bu yıl içindeyse tekrar üretilmez — hâlâ "yılda bir kez" garantisi var,
      yalnızca "hangi ay" şartı gevşetildi.

    Her platform HER ZAMAN yeni liste alır (apple-critical §3). İzole hata yönetimi.
    Dönüş: {outcome, rules_processed, playlists_ok, errors} — cron run_log'a yazar.
    """
    db = _db()
    now = datetime.now(timezone.utc)
    year, month = _previous_month()
    prev_year = now.year - 1
    logger.info("Auto-playlist job: month=%s %d, yearly_target=%d", MONTH_NAMES[month], year, prev_year)

    rules_resp = (
        db.table("auto_playlist_rules")
        .select(
            "id, user_id, rule_type, track_count, target_platforms, "
            "name_format, enabled, last_run_at, sort_by"
        )
        .eq("enabled", True)
        .in_("rule_type", ["top_month", "top_year"])
        .execute()
    )
    rules = rules_resp.data or []
    logger.info("Found %d active rules", len(rules))

    processed = 0
    playlists_ok = 0
    errors = 0

    for rule in rules:
        rule_id = rule["id"]
        user_id = rule["user_id"]
        n = rule["track_count"]
        rule_type = rule.get("rule_type", "top_month")
        # Kullanıcının seçtiği sıralama ölçütü. `or "plays"`: kolon NOT NULL
        # varsayılanlı ama eski satırlar/kısmi select null döndürebilir ve
        # `rule.get(..., "plays")` null'ı "yok" saymaz, null olarak geçirirdi.
        sort_by = rule.get("sort_by") or "plays"
        target_platforms: list[str] = rule.get("target_platforms") or []

        if not target_platforms:
            logger.info("Rule %s has no target platforms, skipping", rule_id)
            continue

        # Bu dönemde başarılı üretim varsa atla; başarısız denemeler yeniden dener.
        if _already_produced_this_period(db, rule_id, rule_type, now):
            logger.info("Rule %s (%s) already produced this period, skipping", rule_id, rule_type)
            continue

        try:
            if rule_type == "top_year":
                tracks = top_year(user_id, prev_year, n=n, sort_by=sort_by)
                playlist_name = _year_playlist_name(prev_year)
                cover_path = _cover_path_for("top_year", month)
            else:
                tracks = top_month(user_id, year, month, n=n, sort_by=sort_by)
                playlist_name = _month_playlist_name(year, month)
                cover_path = _cover_path_for("top_month", month)
        except Exception as exc:
            logger.error("Rule engine failed for rule %s: %s", rule_id, exc)
            errors += 1
            continue

        if not tracks:
            logger.info("No tracks for rule %s (user %s)", rule_id, user_id)
            continue

        logger.info(
            "Rule %s (%s): '%s' — %d tracks → %s",
            rule_id, rule_type, playlist_name, len(tracks), target_platforms,
        )

        try:
            pushed = await _push_rule_to_platforms(
                db, rule_id=rule_id, user_id=user_id, playlist_name=playlist_name,
                tracks=tracks, target_platforms=target_platforms, cover_path=cover_path,
            )
            if pushed > 0:
                processed += 1
                playlists_ok += pushed
                db.table("auto_playlist_rules").update(
                    {"last_run_at": now.isoformat()}
                ).eq("id", rule_id).execute()
            else:
                errors += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("Rule %s push failed: %s", rule_id, exc)
            errors += 1

    outcome = "success" if errors == 0 else ("partial" if processed > 0 else "error")
    logger.info("Auto-playlist job complete: processed=%d ok=%d errors=%d", processed, playlists_ok, errors)
    return {
        "outcome": outcome,
        "rules_processed": processed,
        "playlists_ok": playlists_ok,
        "errors": errors,
    }

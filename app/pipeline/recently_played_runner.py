"""Spotify recently-played sync akışı — her aktif bağlantı için pagination fetch
→ matcher eşleştirme → play_events yazımı + yeni track/ISRC INSERT.
"""
from __future__ import annotations

import logging
from typing import Any

from app.matching.matcher import match_track
from app.matching.repo import SupabaseTrackRepo
from app.services import api_gate
from app.services.logger import log_event
from app.services.spotify_allowlist import mark_access
from app.services.spotify_recently_played import (
    SpotifyAuthError,
    SpotifyForbiddenError,
    SpotifyRateLimitError,
    fetch_recently_played,
)
from app.services.spotify_token import get_valid_spotify_token

logger = logging.getLogger("rosso.worker.recently_played_runner")

# ⚠ Eski `_PROVIDER = "spotify_recently_played"` KALDIRILDI (plan 08, 2026-08-05).
# Ayrı havuz, cezanın hesap geneli olduğu ölçülünce yanlış model çıktı:
# catalog uçları 429 alırken bu havuz "temiz" görünüyor ve istek atmaya devam
# ediyordu. Artık merkezî geçit (`api_gate.SCOPE_USER`) kullanılıyor.


def run_one_recently_played_sync(
    client: Any, http: Any, crypto_key: str, user_id: str | None = None
) -> dict[str, Any]:
    """Aktif Spotify bağlantıları için recently-played sync.

    user_id verilirse YALNIZ o kullanıcı işlenir (anlık tazelik, FAZ 2 — kullanıcı
    siteye girince tetiklenir). Verilmezse tüm aktif bağlantılar (cron davranışı).
    Mantık tek yerde — cron ile anlık tetik aynı runner'ı kullanır.
    {outcome, users_processed, events_written, errors} döner.
    """
    # ── Merkezî geçit (plan 08 / 2026-08-05) ────────────────────────────────
    # ⚠ Bu runner ESKİDEN kendi ayrı havuzunu kullanıyordu
    # (`spotify_recently_played`). Ölçüm cezanın HESAP GENELİ olduğunu
    # gösterdi: catalog uçları 429 alırken bu da aynı kotayı yiyor. İki havuz
    # birbirinden habersizken biri ceza yerken diğeri istek atmaya devam
    # ediyordu — merkezî geçit ikisini tek mantıkta birleştirir.
    #
    # Öncelik HIGH: Dashboard'un İLK gördüğü bölüm "Son Dinlenenler".
    # Bütçe kısıtlandığında paket içeriğinden sonraki sırayı bu alır.
    gate = api_gate.check(client, api_gate.SCOPE_USER, priority=api_gate.PRIORITY_HIGH)
    if not gate.allowed:
        logger.warning(
            "Spotify recently-played: geçit kapalı (%s) — atlanıyor", gate.reason
        )
        return {
            "outcome": "blocked" if gate.reason == "blocked" else "skipped",
            "users_processed": 0,
            "events_written": 0,
            "gate_reason": gate.reason,
        }

    q = (
        client.table("platform_connections")
        .select("user_id, access_token, last_recently_played_sync_at")
        .eq("platform", "spotify")
        .eq("is_active", True)
    )
    if user_id is not None:
        q = q.eq("user_id", user_id)
    res = q.execute()
    connections = res.data or []
    if not connections:
        return {"outcome": "empty", "users_processed": 0, "events_written": 0}

    repo = SupabaseTrackRepo(client)
    users_processed = 0
    events_written = 0
    errors = 0          # S1: atlanan kullanıcı = gerçek arıza, sessiz kalmasın
    interrupted = False  # 429 batch'i yarıda kesti mi

    for conn in connections:
        user_id = conn["user_id"]
        token = get_valid_spotify_token(client, user_id, crypto_key, http)
        if not token:
            # S1: token alınamadı/yenilenemedi = arıza. Atla ama SAY.
            logger.warning("Spotify token alınamadı: user=%s — atlanıyor", user_id)
            log_event(operation="spotify_recently_played", severity="error",
                      user_id=user_id, platform="spotify", error_code="token_refresh_failed",
                      error_message="Token alınamadı/yenilenemedi — kullanıcı bu turda atlandı")
            errors += 1
            continue

        after_ms = None
        last_sync = conn.get("last_recently_played_sync_at")
        if last_sync:
            from datetime import datetime
            try:
                dt = datetime.fromisoformat(last_sync.replace("Z", "+00:00"))
                after_ms = int(dt.timestamp() * 1000)
            except (ValueError, AttributeError):
                after_ms = None

        try:
            items = fetch_recently_played(token, after_ms, http)
        except SpotifyAuthError:
            # 401: Token geçersiz/süresi dolmuş. Hemen pes edip is_active=False YAPMA.
            # Self-Healing: force_refresh ile yeni token alıp hemen tekrar dene.
            logger.info("Spotify 401 alindi, token zorla yenileniyor: user=%s", user_id)
            fresh_token = get_valid_spotify_token(client, user_id, crypto_key, http, force_refresh=True)
            if fresh_token:
                try:
                    items = fetch_recently_played(fresh_token, after_ms, http)
                except Exception as retry_exc:  # noqa: BLE001
                    logger.warning("Spotify taze token ile retry basarisiz: user=%s, exc=%s", user_id, retry_exc)
                    errors += 1
                    continue
            else:
                logger.error("Spotify token zorla yenilenemedi: user=%s", user_id)
                try:
                    client.table("platform_connections").update(
                        {"is_active": False}
                    ).eq("user_id", user_id).eq("platform", "spotify").execute()
                except Exception:  # noqa: BLE001
                    pass
                errors += 1
                continue
        except SpotifyForbiddenError:
            # 403: token sağlam, kullanıcı allowlist'te değil. is_active'i BOZMA
            # (401 değil bu) — sadece bu kullanıcıyı atla, cron diğerleriyle devam etsin.
            logger.warning("Spotify 403 (allowlist dışı): user=%s — atlanıyor", user_id)
            log_event(operation="spotify_recently_played", severity="error",
                      user_id=user_id, platform="spotify", error_code="spotify_403_forbidden",
                      error_message="Spotify hesabı uygulamanın allowlist'inde değil (Dev Mode)")
            # FAZ 6: gözlenen gerçeği kabul kuyruğuna yaz — admin panelinde
            # "eklendi" görünüp veri akmayan hayalet durum kalmasın.
            mark_access(client, user_id, granted=False)
            errors += 1  # S1: atlandı ama sayıldı — outcome dürüst kalsın
            continue
        except SpotifyRateLimitError as exc:
            # ⚠ Varsayılan 60 sn DEĞİL 1 saat (kural §1): ölçüm Spotify'ın
            # 23,86 SAAT ceza verebildiğini gösterdi. `Retry-After` okunamazsa
            # 60 sn beklemek, ceza sürerken tekrar tekrar duvara koşmaktır.
            #
            # `record_429` ayrıca BÜTÇEYİ de kapatır: 429 aldıysak günlük
            # kotayı zaten aşmışız demektir; sayacı olduğu yerde bırakmak
            # ceza bitince aynı hataya koşmak olurdu.
            api_gate.record_429(
                client,
                api_gate.SCOPE_USER,
                exc.retry_after,
                reason="recently_played_429",
            )
            interrupted = True  # kalan kullanıcılar bu turda hiç denenmedi
            break

        # FAZ 6: 403 almadan buraya geldiysek erişim KANITLANDI (boş liste bile olsa
        # — "yeni şarkı yok" demek, "erişemiyorum" demek değil).
        mark_access(client, user_id, granted=True)
        users_processed += 1
        if not items:
            continue

        latest_played_at = None
        for item in items:
            track_id = _resolve_or_create_track(client, repo, item)
            if not track_id:
                continue
            # ZIP-örtüşme koruması (0104 bulgusu, 2026-07-17): ZIP export'u ile
            # bu runner aynı çalmayı farklı played_at hassasiyetiyle (saniyenin
            # altı) yazınca play_events_dedup_idx (tam eşleşme) yakalayamıyor —
            # 328 çift kayıt, ~20 saat fazla dinleme süresi yaratmıştı (Ferzan).
            # Yazmadan önce aynı track için ±5sn içinde satır var mı kontrol et.
            if _has_nearby_play(client, user_id, track_id, item["played_at"]):
                continue
            try:
                client.table("play_events").upsert({
                    "user_id": user_id,
                    "track_id": track_id,
                    "played_at": item["played_at"],
                    "platform": "spotify",
                    "source": "api_realtime",
                    # Spotify recently-played API gerçek dinlenen süreyi (ms_played) vermez
                    # — track'in tam süresini (duration_ms) fallback olarak kullan (Fable 5
                    # keşif bulgusu, 2026-07-02: sabit 0 recap/istatistik modüllerini bozar).
                    "ms_played": item.get("duration_ms", 0),
                }, on_conflict="user_id,played_at,track_id").execute()
                events_written += 1
            except Exception:  # noqa: BLE001
                logger.warning("play_event yazılamadı: user=%s track=%s", user_id, track_id)
            # B2: Spotify recently-played newest-first döner → son item EN ESKİ'dir.
            # Damgayı en YENİ played_at'e taşımak için max() kullan; yoksa damga geriye
            # gider ve her tur aynı eski şarkılar boşuna tekrar çekilir.
            played_at = item["played_at"]
            if latest_played_at is None or played_at > latest_played_at:
                latest_played_at = played_at

        if latest_played_at:
            try:
                client.table("platform_connections").update({
                    "last_recently_played_sync_at": latest_played_at,
                }).eq("user_id", user_id).eq("platform", "spotify").execute()
            except Exception:  # noqa: BLE001
                logger.warning("last_recently_played_sync_at güncellenemedi: user=%s", user_id)

    # S1 (ytmusic_history_runner deseni): arıza sağlıklı raporlanmasın.
    # Hiç kullanıcı işlenemediyse ve arıza varsa 'error'; kısmi arıza/kesinti 'partial'.
    if errors > 0 and users_processed == 0:
        outcome = "error"
    elif errors > 0 or interrupted:
        outcome = "partial"
    else:
        outcome = "success"

    return {
        "outcome": outcome,
        "users_processed": users_processed,
        "events_written": events_written,
        "errors": errors,
    }


_NEARBY_WINDOW_SECONDS = 5


def _has_nearby_play(client: Any, user_id: str, track_id: str, played_at: str) -> bool:
    """Aynı track için played_at'e ±5sn içinde zaten bir satır var mı (0104).

    ZIP export'u ile bu runner aynı çalmayı farklı saniye-altı hassasiyetiyle
    yazabiliyor; tam eşleşme arayan play_events_dedup_idx bunu yakalamaz. Bu
    kontrol saniye içi çakışmayı runner tarafında engeller — DB'ye asla
    girmeden. Sorgu hatası olursa (geçici) fail-open: yazmaya izin ver, tek
    play_event'in kaybolması dedup'tan daha kötü bir sonuç olurdu.
    """
    from datetime import datetime, timedelta

    try:
        dt = datetime.fromisoformat(played_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False
    lo = (dt - timedelta(seconds=_NEARBY_WINDOW_SECONDS)).isoformat()
    hi = (dt + timedelta(seconds=_NEARBY_WINDOW_SECONDS)).isoformat()
    try:
        res = (
            client.table("play_events")
            .select("id")
            .eq("user_id", user_id)
            .eq("track_id", track_id)
            .gte("played_at", lo)
            .lte("played_at", hi)
            .limit(1)
            .execute()
        )
        return bool(res.data)
    except Exception:  # noqa: BLE001
        return False


def _resolve_or_create_track(client: Any, repo: SupabaseTrackRepo, item: dict) -> str | None:
    """Spotify track'i tracks tablosunda bul; yoksa yeni satır ekle.

    ISRC hakkında (B8): recently-played ucu ISRC VERMİYOR — `item["isrc"]` pratikte
    hep None'dır (canlı doğrulama 2026-07-11: 5/5 şarkıda `external_ids` yok). Yani
    aşağıdaki ISRC yazma dalı gerçekte hiç çalışmıyor. Dal yine de duruyor: Spotify
    alanı geri getirirse bedavaya doğru çalışsın. ISRC'nin gerçek kaynağı ZIP export
    ve genre zenginleştirmesidir.

    Dal çalışırsa güvenlidir: repo.py'nin SELECT'i isrc döndürmez (find_by_spotify_id
    yalnız id/title/artists/duration_ms çeker), bu yüzden hit["isrc"] güvenilmez.
    repo.py DEĞİŞTİRİLMEDEN, güncelleme `.is_("isrc", "null")` koşuluyla yapılır:
    DB'de ISRC gerçekten NULL ise yazılır, doluysa (başka bir cron'un yazdığı değer)
    ÜZERİNE YAZILMAZ (Fable 5 keşif bulgusu, 2026-07-02).
    """
    # Çöp guard'ı (P0, 2026-07-03): title/artists/spotify_id boşsa insert etme.
    # (recently-played yanıtı şu an doğru şekilli, ama şema kaymasına karşı koruma.)
    if not item.get("spotify_id") or not item.get("title") or not item.get("artists"):
        return None
    hit = repo.find_by_spotify_id(item["spotify_id"]) if item.get("spotify_id") else None
    if hit:
        if item.get("isrc"):
            try:
                client.table("tracks").update({"isrc": item["isrc"]}).eq(
                    "id", hit["id"]
                ).is_("isrc", "null").execute()
            except Exception:  # noqa: BLE001
                pass

        # Kapak — plan 08 (2026-08-05). Görsel yanıtta ZATEN geliyordu ve
        # atılıyordu; kör dolgu cron'u sonra AYNI görseli ayrı istekle
        # topluyordu. Burada yazmak ek Spotify isteği getirmez.
        #
        # ⚠ `.is_("image_url","null")` — mevcut kapağı EZMEZ. Kendi Storage
        # kopyamız (kalıcı, boyutlandırılabilir) Spotify CDN URL'inden
        # değerlidir; onu geçici bir CDN linkiyle değiştirmek gerileme olurdu.
        if item.get("image_url"):
            try:
                client.table("tracks").update({"image_url": item["image_url"]}).eq(
                    "id", hit["id"]
                ).is_("image_url", "null").execute()
            except Exception:  # noqa: BLE001
                pass
        return hit["id"]

    try:
        # duration_ms ELİMİZDE VARDI ama yazılmıyordu (2026-07-12 bulgusu):
        # spotify_recently_played.py bu alanı çekiyor, burada atılıyordu →
        # canlıda duration_ms 11.692/11.692 NULL. Artık yazılıyor; katalog dolgu
        # cron'u (catalog_backfill) da eski satırları toplar.
        row: dict[str, Any] = {
            "spotify_id": item.get("spotify_id"),
            "isrc": item.get("isrc"),
            "title": item.get("title", ""),
            "artists": item.get("artists", []),
        }
        if item.get("duration_ms"):
            row["duration_ms"] = item["duration_ms"]
        # Kapak ilk kayıtta yazılır (plan 08) — yeni dinlenen şarkı Dashboard'un
        # "Son Dinlenenler" bölümünde görselli belirir, kör dolgu beklenmez.
        if item.get("image_url"):
            row["image_url"] = item["image_url"]
        res = client.table("tracks").insert(row).execute()
        rows = res.data or []
        return rows[0]["id"] if rows else None
    except Exception:  # noqa: BLE001
        # B4: INSERT çakışması (UNIQUE spotify_id) = başka bir cron/tur aynı track'i
        # araya ekledi. None dönmek dinlemeyi kaybettirir — tekrar OKU, id'yi döndür.
        hit = repo.find_by_spotify_id(item["spotify_id"])
        if hit:
            return hit["id"]
        logger.warning("Yeni track INSERT başarısız + tekrar okuma boş: spotify_id=%s", item.get("spotify_id"))
        return None

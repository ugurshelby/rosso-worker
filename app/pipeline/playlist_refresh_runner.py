"""Playlist tazeleme akışı — snapshot_id ucuz kontrolü, değişmişse tam senkron.

NotebookLM doğrulaması: snapshot_id playlist değişikliğinin ucuz göstergesi.
Fable 5 keşif bulgusu (2026-07-02, iki kritik düzeltme):
  1. fetch_user_playlists yanıtı zaten her playlist için snapshot_id taşıyor
     — ayrı bir fetch_playlist_snapshot_id GET'i GEREKSİZ, kullanılmaz.
  2. playlist_tracks tablosunun gerçek UNIQUE constraint'i (playlist_id,
     position) — (playlist_id, track_id) DEĞİL. upsert(on_conflict=
     "playlist_id,track_id") yanlış constraint hedefler ve pozisyon
     çakışmalarında hata verir. Doğru yöntem: playlist değiştiyse önce
     TÜM mevcut playlist_tracks satırlarını sil, sonra sırayla yeniden ekle.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.matching.repo import SupabaseTrackRepo
from app.services import api_gate
from app.services.logger import log_event
from app.services.spotify_playlists import fetch_playlist_items, fetch_user_playlists
from app.services.spotify_token import get_valid_spotify_token

logger = logging.getLogger("rosso.worker.playlist_refresh_runner")


def run_one_playlist_refresh(
    client: Any, http: Any, crypto_key: str, user_id: str | None = None
) -> dict[str, Any]:
    """Aktif Spotify bağlantıları için playlist tazeleme.

    user_id verilirse YALNIZ o kullanıcı (anlık tazelik, FAZ 2); verilmezse tüm
    aktif bağlantılar (cron). Mantık tek yerde. {outcome, playlists_updated, errors}.
    """
    # ── Merkezî geçit (plan 08 / 2026-08-05) ────────────────────────────────
    # Bu runner ESKİDEN korumasızdı: 250ms geçidi vardı ama ne cooldown
    # kontrolü ne bütçe farkındalığı. Ölçüm (2026-08-05) sınırın HIZ değil
    # HACİM olduğunu gösterdi (398 istekte 429, ceza 23,86 SAAT) — yani hız
    # geçidi tek başına hiçbir şey korumuyordu.
    #
    # Öncelik NORMAL: playlist tazeleme kullanıcının o an baktığı bir şey
    # değil (12 saatte bir cron). Bütçe azaldığında üst katlara yol verir.
    #
    # ⚠ Anlık tazelik yolunda (user_id verilmiş = kullanıcı ekranda bekliyor)
    # öncelik HIGH'a çıkar — aynı kod iki farklı bağlamda çalışıyor.
    priority = api_gate.PRIORITY_HIGH if user_id is not None else api_gate.PRIORITY_NORMAL
    gate = api_gate.check(client, api_gate.SCOPE_USER, priority=priority)
    if not gate.allowed:
        logger.info(
            "Playlist tazeleme: geçit kapalı (%s) — tur atlandı, kota korunuyor",
            gate.reason,
        )
        return {
            "outcome": "blocked" if gate.reason == "blocked" else "skipped",
            "playlists_updated": 0,
            "gate_reason": gate.reason,
        }

    q = (
        client.table("platform_connections")
        .select("user_id, access_token")
        .eq("platform", "spotify")
        .eq("is_active", True)
    )
    if user_id is not None:
        q = q.eq("user_id", user_id)
    res = q.execute()
    connections = res.data or []
    if not connections:
        # İstek atılmadı → ayrılan bütçeyi iade et (sayaç gerçeği göstersin).
        api_gate.refund(client, api_gate.SCOPE_USER)
        return {"outcome": "empty", "playlists_updated": 0}

    repo = SupabaseTrackRepo(client)
    updated = 0
    deleted = 0            # SBA-7: Spotify'da silinen → DB'den temizlenen playlist
    errors = 0             # S2: token'sız kullanıcı = arıza, sessiz kalmasın
    skipped_playlists = 0  # S2: 403/404 ile atlanan listeler görünür olsun
    skipped_users = 0      # S3: erişilemeyen (allowlist dışı) kullanıcılar

    for conn in connections:
        user_id = conn["user_id"]
        token = get_valid_spotify_token(client, user_id, crypto_key, http)
        if not token:
            logger.warning("Spotify token alınamadı (playlist): user=%s — atlanıyor", user_id)
            log_event(operation="playlist_refresh", severity="error",
                      user_id=user_id, platform="spotify", error_code="token_refresh_failed",
                      error_message="Token alınamadı — playlist'ler bu turda tazelenmedi")
            errors += 1
            continue

        # B6: Kullanıcının kendi Spotify ID'sini bir kez al; yalnız SAHİBİ olduğu
        # playlist'leri işle. Takip edilen (başkasının) listeler item fetch'te 403
        # verir ve eskiden tüm cron'u kırardı.
        #
        # 🔴 2026-07-29: `_fetch_me_id` başarılıysa DB'ye yaz. Ferzan'ın kaydı
        # 10 Tem'de (kimlik yakalama kodu eklenmeden önce) açıldığı için
        # `spotify_user_id` NULL'dı → cron HER turda boşuna /v1/me atıyordu.
        # Değer elimizdeyken saklamak §1.6 gereği (gereksiz istek = kota yeme).
        me_id = _fetch_me_id(token, http, client=client, user_id=user_id)

        # 🔴 2026-07-29 (S3): Kullanıcı seviyesinde 403 → SADECE O KULLANICI atlanır.
        # Eskiden burada `raise_for_status()` yukarı fırlıyor ve TÜM cron çöküyordu:
        # Yankı allowlist dışı olduğu için ("The user is not registered for this
        # application") Ferzan'ın ve Efendim'in playlist'leri de tazelenmiyordu.
        # Canlı kanıt: pipeline_runs outcome=error.
        # B1 (liste bazlı 403) zaten vardı ama kullanıcı bazlı olan eksikti.
        try:
            remote_playlists = fetch_user_playlists(token, http)
        except Exception as exc:  # noqa: BLE001
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (401, 403, 404):
                logger.warning(
                    "Spotify playlist listesi alınamadı (HTTP %s) — user=%s ATLANDI, tur sürüyor",
                    status, user_id,
                )
                log_event(operation="playlist_refresh", severity="warn",
                          user_id=user_id, platform="spotify",
                          error_code=f"user_playlists_http_{status}",
                          error_message=(
                              "Kullanıcının playlist'leri alınamadı (erişim yok — "
                              "Spotify Dev Mode allowlist'te olmayabilir); bu tur atlandı"
                          ))
                skipped_users += 1
                continue

            # ── 429: ceza damgası + bütçe kapatma (plan 08, 2026-08-05) ──
            # ⚠ ESKİDEN BU DAL YOKTU: 429 `raise` ile yukarı gidiyor, cron
            # çöküyor ama cooldown YAZILMIYORDU. Sonraki tur hiçbir şey
            # bilmeden yeniden istek atıyor, cezayı tazeliyordu.
            #
            # Ölçüm (2026-08-05): ceza 23,86 SAAT. Damga vurulmazsa diğer
            # Spotify işleri de körlemesine duvara koşar.
            if status == 429:
                retry_after = getattr(getattr(exc, "response", None), "headers", {}).get(
                    "Retry-After"
                )
                api_gate.record_429(
                    client,
                    api_gate.SCOPE_USER,
                    float(retry_after) if retry_after else None,
                    reason="playlist_refresh_429",
                )
                logger.warning(
                    "Playlist tazeleme: 429 — ceza damgası vuruldu, tur durduruluyor"
                )
                break  # turu bitir; DB'ye yazılanlar korunur

            raise  # beklenmedik hata (5xx, ağ) yukarı gitsin — cron kaydına yansısın
        if me_id:
            remote_playlists = [
                rp for rp in remote_playlists
                if rp.get("owner_id") is None or rp.get("owner_id") == me_id
            ]
        existing_res = (
            client.table("playlists")
            .select("id, platform_id, snapshot_id, cover_url, description")
            .eq("user_id", user_id)
            .eq("platform", "spotify")
            .execute()
        )
        existing_by_platform_id = {p["platform_id"]: p for p in (existing_res.data or [])}

        # SBA-7 (2026-07-26): Spotify'da SİLİNEN playlist'ler DB'de hayalet
        # kalıyordu. Runner yalnız uzak listeler üzerinde döner (upsert), silineni
        # hiç görmediği için DB satırı sonsuza dek durur. Bu tur Spotify'dan dönen
        # platform_id setini topla; döngü sonunda bu sette OLMAYAN DB satırlarını
        # sil. ⚠ Güvenlik kapıları aşağıda (reconcile bloğu) — körü körüne silmek
        # canlı playlist'leri yok eder.
        remote_platform_ids = {rp["spotify_id"] for rp in remote_playlists}

        for rp in remote_playlists:
            platform_id = rp["spotify_id"]
            existing = existing_by_platform_id.get(platform_id)

            # fetch_user_playlists yanıtındaki snapshot_id zaten güncel — ayrı
            # bir GET ile teyide gerek yok (Fable 5 keşif bulgusu).
            if existing and existing.get("snapshot_id") == rp["snapshot_id"]:
                # 🔴 SBA-3 tuzağı (2026-07-21): buradaki erken `continue` upsert'i
                # de atlıyor. Yani kapak/açıklama yazımını eklemek TEK BAŞINA
                # yetmezdi — mevcut 105 playlist'in snapshot'ı değişmediği için
                # alanlar sonsuza dek NULL kalırdı ve "düzelttim" demiş olurdum.
                #
                # Çözüm: içerik değişmemişse item fetch YİNE yapılmaz (pahalı
                # olan o), ama DB'de eksik olan metadata varsa ucuz bir upsert
                # atılır. Veri zaten elimizde — fetch_user_playlists'ten geldi,
                # EK İSTEK YOK.
                eksik_metadata = (
                    (rp.get("cover_url") and not existing.get("cover_url"))
                    or (rp.get("description") and not existing.get("description"))
                )
                if eksik_metadata:
                    client.table("playlists").upsert({
                        "user_id": user_id,
                        "platform": "spotify",
                        "platform_id": platform_id,
                        "name": rp["name"],
                        "snapshot_id": rp["snapshot_id"],
                        "track_count": rp["track_count"],
                        "cover_url": rp.get("cover_url"),
                        "description": rp.get("description"),
                        # synced_at'e DOKUNMA: içerik senkronu olmadı, yalnız
                        # metadata tamamlandı. Yazsaydık UI "az önce senkronlandı"
                        # der, kullanıcıya yalan söylerdik.
                    }, on_conflict="user_id,platform,platform_id").execute()
                continue  # değişmemiş — item fetch YAPMA

            # B1: Tek bir playlist'in item fetch'i 403/404 verirse (allowlist dışı,
            # silinmiş, gizli) o playlist'i ATLA — cron asla tek liste yüzünden ölmesin.
            try:
                items = fetch_playlist_items(token, platform_id, http)
            except Exception as exc:  # noqa: BLE001
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status in (403, 404):
                    logger.warning(
                        "playlist item fetch atlandı (HTTP %s): user=%s playlist=%s",
                        status, user_id, platform_id,
                    )
                    skipped_playlists += 1  # S2: atlandı ama SAYILDI
                    log_event(operation="playlist_refresh", severity="warn",
                              user_id=user_id, platform="spotify",
                              error_code=f"playlist_http_{status}", related_id=platform_id,
                              error_message="Playlist item fetch atlandı (erişilemez/silinmiş liste)")
                    continue
                raise  # beklenmedik hata (5xx vb.) yukarı gitsin — cron kaydına yansısın

            playlist_res = client.table("playlists").upsert({
                "user_id": user_id,
                "platform": "spotify",
                "platform_id": platform_id,
                "name": rp["name"],
                "snapshot_id": rp["snapshot_id"],
                "track_count": rp["track_count"],
                # Ayrıştırıcı bunları zaten aynı yanıttan alıyor (ek istek yok).
                # .get() ile okunuyor: eski çağrı yolları bu anahtarları
                # göndermezse upsert kırılmasın, alan NULL kalsın.
                "cover_url": rp.get("cover_url"),
                "description": rp.get("description"),
                "synced_at": datetime.now(timezone.utc).isoformat(),
            }, on_conflict="user_id,platform,platform_id").execute()
            playlist_rows = playlist_res.data or []
            playlist_db_id = playlist_rows[0]["id"] if playlist_rows else (existing or {}).get("id")

            if playlist_db_id:
                # UNIQUE(playlist_id, position) — upsert yerine sil + yeniden ekle.
                try:
                    client.table("playlist_tracks").delete().eq("playlist_id", playlist_db_id).execute()
                except Exception:  # noqa: BLE001
                    logger.warning("playlist_tracks silme başarısız: playlist=%s", playlist_db_id)

                for item in items:
                    track_id = _resolve_or_create_track(client, repo, item)
                    if not track_id:
                        continue
                    try:
                        client.table("playlist_tracks").insert({
                            "playlist_id": playlist_db_id,
                            "track_id": track_id,
                            "position": item["position"],
                            "added_at": item.get("added_at"),
                        }).execute()
                    except Exception:  # noqa: BLE001
                        logger.warning("playlist_tracks insert başarısız: playlist=%s", playlist_db_id)

            updated += 1

        # SBA-7 reconcile: Spotify'da artık olmayan (kullanıcının sildiği)
        # playlist'leri DB'den temizle. GÜVENLİK KAPILARI (§1.5 — körü körüne
        # silmek 178 canlı playlist'i yok edebilir):
        #   1. me_id alınamadıysa → sahiplik filtresi uygulanmadı, remote set
        #      güvenilmez → SİLME.
        #   2. remote_playlists BOŞ döndüyse → API geçici arıza vermiş olabilir,
        #      boş sete güvenip her şeyi silmek felaket → SİLME.
        # Yalnız iki kapı da geçilirse, remote sette olmayan DB satırları silinir.
        if me_id and remote_playlists:
            stale_ids = [
                p["platform_id"]
                for p in (existing_res.data or [])
                if p["platform_id"] not in remote_platform_ids
            ]
            if stale_ids:
                try:
                    client.table("playlists").delete().eq(
                        "user_id", user_id
                    ).eq("platform", "spotify").in_(
                        "platform_id", stale_ids
                    ).execute()
                    deleted += len(stale_ids)
                    logger.info(
                        "Spotify'da olmayan %d playlist DB'den silindi: user=%s",
                        len(stale_ids), user_id,
                    )
                    log_event(operation="playlist_refresh", severity="info",
                              user_id=user_id, platform="spotify",
                              error_code="stale_playlists_removed",
                              error_message=f"{len(stale_ids)} silinmiş playlist DB'den temizlendi")
                except Exception:  # noqa: BLE001
                    logger.warning("stale playlist silme başarısız: user=%s", user_id)

    # S2/S3: arıza/atlama sağlıklı raporlansın. Token'sız kullanıcı varken hiç iş
    # yapılamadıysa 'error'; kısmi arıza, atlanan liste ya da atlanan kullanıcı
    # varsa 'partial'. Atlanan kullanıcı 'error' DEĞİL — sistem doğru çalışıyor,
    # o kişi Spotify tarafında erişilemez (allowlist). Panelde görünür kalsın diye
    # 'partial'; "success" deyip yutmak §1.65'teki sessiz kayıp tuzağı olurdu.
    if errors > 0 and updated == 0:
        outcome = "error"
    elif errors > 0 or skipped_playlists > 0 or skipped_users > 0:
        outcome = "partial"
    else:
        outcome = "success"

    return {
        "outcome": outcome,
        "playlists_updated": updated,
        "playlists_deleted": deleted,
        "errors": errors,
        "skipped_playlists": skipped_playlists,
        "skipped_users": skipped_users,
    }


def _fetch_me_id(
    token: str, http: Any, client: Any = None, user_id: str | None = None
) -> str | None:
    """Kullanıcının Spotify hesap ID'sini /v1/me'den al (sahiplik filtresi için).

    Başarısız olursa None döner — o durumda filtre uygulanmaz (güvenli taraf:
    hepsini işle, eski davranış). B1 sayesinde 403 veren liste zaten atlanır.

    2026-07-29: `client`+`user_id` verilirse ve DB'de `spotify_user_id` boşsa
    değer kaydedilir. Ferzan'ın bağlantısı kimlik yakalama kodundan (11 Tem)
    ÖNCE açıldığı için NULL'dı ve her tur boşuna /v1/me isteği atılıyordu.
    Yazma hatası akışı bozmaz — ID zaten elimizde, senkron devam eder.
    """
    try:
        resp = http.get(
            "https://api.spotify.com/v1/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        me_id = resp.json().get("id")
    except Exception:  # noqa: BLE001
        logger.warning("Spotify /me alınamadı — sahiplik filtresi bu tur atlandı")
        return None

    if me_id and client is not None and user_id:
        try:
            client.table("platform_connections").update(
                {"spotify_user_id": me_id}
            ).eq("user_id", user_id).eq("platform", "spotify").is_(
                "spotify_user_id", "null"
            ).execute()
        except Exception:  # noqa: BLE001
            logger.warning("spotify_user_id kaydedilemedi: user=%s", user_id)

    return me_id


def _resolve_or_create_track(client: Any, repo: SupabaseTrackRepo, item: dict) -> str | None:
    # Çöp guard'ı (P0, 2026-07-03): title/artists/spotify_id boşsa insert etme.
    # Spotify şema kayması boş item üretebilir; bunlar dedup edilemez ve her
    # cron turunda yeni çöp satır oluşturur.
    if not item.get("spotify_id") or not item.get("title") or not item.get("artists"):
        return None
    hit = repo.find_by_spotify_id(item["spotify_id"]) if item.get("spotify_id") else None
    if hit:
        return hit["id"]
    try:
        res = client.table("tracks").insert({
            "spotify_id": item.get("spotify_id"),
            "isrc": item.get("isrc"),
            "title": item.get("title", ""),
            "artists": item.get("artists", []),
        }).execute()
        rows = res.data or []
        return rows[0]["id"] if rows else None
    except Exception:  # noqa: BLE001
        # B4: INSERT çakışması = başka bir cron/tur aynı track'i araya ekledi.
        # None dönmek playlist_tracks satırını kaybettirir — tekrar OKU, id'yi döndür.
        hit = repo.find_by_spotify_id(item["spotify_id"])
        if hit:
            return hit["id"]
        logger.warning("track INSERT başarısız + tekrar okuma boş: spotify_id=%s", item.get("spotify_id"))
        return None

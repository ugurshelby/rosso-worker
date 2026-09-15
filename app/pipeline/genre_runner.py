"""Genre DNA enrichment akışı — çok kaynak, ağırlıklı, hiyerarşik.

Genre kaynağı: Deezer + Last.fm + MusicBrainz (hepsi kotasız/ISRC'siz, elimizdeki
artist+title ile çalışır). Spotify Dev Mode kotası genre için kullanılamaz.

Her track için:
  1. Track düzeyi: Deezer track + Last.fm track + MB recording → merge → 3 slot.
  2. Boş slot(lar) varsa: artist profilinden (get_or_build_artist_profile) doldur.
  3. genre_data (jsonb) + genres[] (slots ile senkron) + genre_source yaz.
  4. Her şey boşsa genre_lookup_failed_at (kalıcı, retry yok).

genre_data formatı: {slots, weights, raw_scores, sources} — bkz. genre_normalize.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.services import cooldown
from app.services.artist_profile import (
    _MIN_TRACKS_FOR_PROFILE,
    get_db_artist_track_count,
    get_or_build_artist_profile,
    is_artist_match,
)
from app.services.deezer_genre import get_deezer_track_scored_with_anchor
from app.services.genre_errors import RateLimitError
from app.services.genre_normalize import merge_scores, normalize_genres, select_slots
from app.services.title_clean import clean_title

logger = logging.getLogger("rosso.worker.genre_runner")

_ASYNC_CONCURRENCY = 8  # eş zamanlı track (MB rate-limit'i düşük tutar)
_TRACK_SLOTS = 3


def build_track_genre_data(
    artist: str, title: str, *, lastfm_key: str, http: Any
) -> tuple[dict, str | None, int | None, tuple[str, float | None] | None]:
    """Track için ağırlıklı genre_data + çapa(ad+id) + rate-limit sinyali üret.

    İki aşamalı arama: önce ham başlık, boş dönerse feat. temizlenip tekrar.
    Döner: (genre_data, anchor_name, anchor_id, rate_limited).
      - anchor_id: track çapasının doğrulanmış Deezer artist.id'si. Artist DNA'sı
        bu id'den (isimle arama yapmadan) çözülür — Deezer isim araması yanlış
        sanatçı buluyor (Motive→Harp, Ceza→Brezilya) (2026-07-04).
      - rate_limited = (provider, retry_after) ise 429 alındı; çağıran set_cooldown
        besler, track lookup_failed YAZMAZ.
    """
    empty = {"slots": [], "weights": [], "raw_scores": {}, "sources": {}}
    if not artist:
        return empty, None, None, None

    clean = clean_title(title)
    scored_lists = []
    anchor: str | None = None
    anchor_id: int | None = None

    # ── Track-seviyesi SADECE Deezer (2026-07-03, BÖLÜM 3) ──
    # Last.fm track (canlı 0/21 — ölü) ve MB recording (4/21, 0 tag, üstelik
    # track başına 1sn rate-limit beklemesi) akıştan ÇIKARILDI. Track-seviyesi
    # değer artık yalnız fold'lu iki aşamalı Deezer aramasında. Last.fm/MB'nin
    # gerçek değeri artist-seviyesinde (artist_profile) korunuyor.
    try:
        dz, anchor, anchor_id = get_deezer_track_scored_with_anchor(artist, title, http)
        if not dz and clean != title:
            dz, anchor, anchor_id = get_deezer_track_scored_with_anchor(artist, clean, http)
        if dz:
            scored_lists.append(normalize_genres(dz, "deezer_track"))
    except RateLimitError as exc:
        return empty, None, None, (exc.provider, exc.retry_after)
    except Exception:  # noqa: BLE001
        logger.warning("Deezer track enrich hatası: %s - %s", artist, title)

    merged = merge_scores(scored_lists)
    return select_slots(merged, max_slots=_TRACK_SLOTS), anchor, anchor_id, None


def fill_empty_slots(track_data: dict, artist_profile: dict) -> dict:
    """Track'in boş slot'larını artist profilindeki AİLE-UYUMLU türlerle doldur.

    **Aile-süzme (isim çakışması koruması):** artist-level Last.fm tag'i isim
    çakışmasına açıktır ("manifest" Türkçe rapçi yerine aynı adlı death metal
    grubunun tag'lerini verebilir). Track-level tür (şarkı adıyla arandığı için
    güvenilir) "gerçeğin çapası"dır. Artist türü, track'in mevcut türlerinden
    HERHANGİ biriyle akraba (are_related) ise eklenir; değilse elenir.

    - Track'in mevcut slot'ları referans türlerdir.
    - Referans yoksa (track tamamen boş) → doldurma yapılmaz; boş track için
      Deezer-taban fallback ayrı çalışır (run_one_genre_batch).
    - Cross-genre türler (instrumental/ambient…) are_related sayesinde daima geçer.
    """
    from app.services.genre_normalize import _SLOT_WEIGHTS, are_related

    slots = list(track_data.get("slots") or [])
    reference = list(slots)  # track'in kendi türleri = aile referansı
    if not reference:
        return track_data  # referans yok → doldurma yok

    profile_slots = artist_profile.get("slots") or []
    for genre in profile_slots:
        if len(slots) >= _TRACK_SLOTS:
            break
        if genre in slots:
            continue
        # Aile-süzme: referans türlerden en az biriyle akraba mı?
        if any(are_related(genre, ref) for ref in reference):
            slots.append(genre)

    weights = _SLOT_WEIGHTS.get(_TRACK_SLOTS, [])[: len(slots)]
    return {
        "slots": slots,
        "weights": weights,
        "raw_scores": track_data.get("raw_scores", {}),
        "sources": track_data.get("sources", {}),
    }


# Aile referansı kurmaya güvenilebilecek artist-level kaynaklar.
# lastfm_artist DAHİL (2026-07-03): artist profiline yazılmış lastfm türü zaten
# resolve_lastfm_trust'tan geçmiştir — SUSPECT (isim çakışması, Motive senaryosu)
# olanlar _build_profile merge'inden çıkarıldığı için profile hiç ulaşmaz. Yani
# profilde görünen lastfm türü TRUSTED'dır ve taban olarak güvenilir.
_TRUSTED_BASE_SOURCES = {"deezer_artist", "db_tracks", "musicbrainz_artist", "lastfm_artist"}


def _build_from_artist_base(artist_profile: dict) -> dict:
    """Track tamamen boşsa artist profilinden GÜVENİLİR taban kur + aile-süzme.

    Aile referansı gerektiği için önce güvenilir kaynaklı (Deezer artist isim
    doğrulamalı / db_tracks / MB artist) bir taban tür seçilir. Bu taban referans
    olur; artist'in kalan (Last.fm dahil) aile-uyumlu türleri eklenir.

    Güvenilir taban yoksa (hepsi sadece lastfm_artist — manifest çakışması gibi)
    boş döner → çağıran lookup_failed yazar.
    """
    profile_sources = artist_profile.get("sources") or {}

    # Güvenilir taban'ı TÜM sources'tan ara (slots hiyerarşi filtresiyle daralmış
    # olabilir; örn. drill/trap seçilince üst tür hip-hop slots'tan düşer ama
    # sources'ta deezer_artist kaynağıyla durur — Yung Ouzo/Cinderella senaryosu).
    base = None
    for genre, srcs in profile_sources.items():
        if set(srcs) & _TRUSTED_BASE_SOURCES:
            base = genre
            break

    if base is None:
        return {"slots": [], "weights": [], "raw_scores": {}, "sources": {}}

    # Taban tür ile başla, profildeki kalan türleri aile-süzme ile ekle
    seed = {"slots": [base], "weights": [], "raw_scores": {}, "sources": {}}
    return fill_empty_slots(seed, artist_profile)


def run_one_genre_batch(
    client: Any,
    settings: Any,
    *,
    batch_limit: int = 50,
    time_budget_s: float = 240.0,
) -> dict[str, Any]:
    """Tek genre batch'i işle: çok kaynak → genre_data + genres yaz.

    time_budget_s: Railway cron penceresi 300 sn ve "önceki tur bitmediyse yeni
    tur ATLANIR" (doküman, 2026-07-16). Bütçe dolunca kalan track'lere HİÇBİR ŞEY
    yazılmaz (lookup_failed dahil) — sonraki tur kaldığı yerden alır. Deezer
    çağrıları _paced_get ile ~4 istek/sn'ye sabitlendiği için büyük batch'in
    süresi öngörülebilir. Döner: {outcome, processed, updated, skipped_deadline}.
    """
    import httpx

    deadline = time.monotonic() + time_budget_s

    # ── 0. Az-track'li sanatçı pending'lerini yeniden aktive et (olay-tabanlı) ───
    # Track sayısı 5'e ulaşmış sanatçıların 'pending' track'lerini işlenebilir yap.
    #
    # ⚠ ESKİ YORUM "ucuz, idempotent tek RPC" DİYORDU — ölçüm çürüttü (2026-08-02):
    # 34 günde 8.067 çağrı · 3.123 sn CPU · ort. 387 ms. Projedeki en pahalı tek
    # iş. Sebebi: her çağrıda 31.636 satır gruplanıyor ve `actual rows=0` —
    # yani hiç iş çıkmıyor. Canlı durum: 1.576 pending sanatçının HİÇBİRİ eşiği
    # geçmiyor (1.164'ü tek track'li, ortalama 1,42 track).
    #
    # Düzeltme RPC tarafında (0194): erken çıkış + JOIN'li sayım.
    #   pending YOKSA  → 0,144 ms (Index Only Scan, hiç hesap yok)
    #   pending VARSA  → 22 ms (eskiden 387 ms ort.)
    #
    # ⚠ Kod tarafında seyreltme (günde bir) DENENDİ ve VAZGEÇİLDİ: zamanlamayı
    # `pipeline_runs`'tan okumak için "son 24 saatte genre_backfill turu var mı"
    # diye bakıyordum — ama o kayıt HER turda yazılıyor, yani koşul ilk turdan
    # sonra hep "yapıldı" der ve iş BİR DAHA HİÇ çalışmazdı. Sessizce ölen bir
    # bakım işi, pahalı çalışandan kötüdür.
    #
    # ✅ 2026-08-11 (migration 0267, Efendim'in kararı): seyreltme sonunda
    # yapıldı ama ZAMANA değil **KATALOĞA** bağlanarak — kapı DB tarafında.
    # Bekleyen sanatçılara son taramadan beri yeni track gelmediyse RPC
    # hiç taramaz. Sinyal işin kendi girdisi olduğu için yukarıdaki
    # "sessizce ölme" tuzağı yok: yeni şarkı geldiği an kapı açılır.
    #   Ölçüldü: kapı açık 32,4 ms / 13.054 blok · kapalı 12,9 ms / 6.941.
    # Buradaki çağrı DEĞİŞMEDİ — her turda çağrılmaya devam eder, kararı
    # RPC verir.
    _reactivate_small_artist_pending(client)

    # ── 1. Cooldown kapısı (her iki HTTP kaynağı da bloklu ise çık) ──────────────
    dz_blocked, _ = cooldown.is_blocked(client, "deezer")
    lf_blocked, _ = cooldown.is_blocked(client, "lastfm")
    if dz_blocked and lf_blocked:
        logger.warning("Genre cron: hem Deezer hem Last.fm bloklu — atlanıyor")
        return {"outcome": "blocked", "processed": 0, "updated": 0}

    # ── 2. genres eksik + başarısız/beklemede işaretlenmemiş track'leri al ───────
    # genre_pending_reason IS NULL: az-track'li sanatçı nedeniyle 'pending' bırakılmış
    # track'ler ATLANIR (sonsuz retry olmaz) — sanatçı track sayısı 5'i geçince
    # olay-tabanlı olarak pending_reason temizlenir ve track burada tekrar görünür.
    res = (
        client.table("tracks")
        .select("id, title, artists")
        .is_("genres", "null")
        .is_("genre_lookup_failed_at", "null")
        .is_("genre_pending_reason", "null")
        .not_.is_("artists", "null")
        .order("created_at")
        .limit(batch_limit)
        .execute()
    )
    tracks = res.data or []
    if not tracks:
        return {"outcome": "empty", "processed": 0, "updated": 0}

    lastfm_key = getattr(settings, "lastfm_api_key", "") or ""

    # ── 3. Track düzeyi enrichment (paralel) → sonra boş slot doldurma ───────────
    with httpx.Client(
        limits=httpx.Limits(max_connections=_ASYNC_CONCURRENCY * 2),
        timeout=httpx.Timeout(20.0),
    ) as http:
        async def _run_batch() -> list[tuple[dict, dict | None, str | None, int | None, tuple | None, bool]]:
            sem = asyncio.Semaphore(_ASYNC_CONCURRENCY)
            loop = asyncio.get_event_loop()

            async def _enrich(t: dict) -> tuple[dict, dict | None, str | None, int | None, tuple | None, bool]:
                artists = t.get("artists") or []
                artist = artists[0] if artists else ""
                title = t.get("title") or ""
                async with sem:
                    # Süre bütçesi doldu → track'e DOKUNMA (skipped=True):
                    # lookup_failed yazılmaz, sonraki tur kaldığı yerden alır.
                    if time.monotonic() > deadline:
                        return t, None, None, None, None, True
                    data, anchor, anchor_id, rl = await loop.run_in_executor(
                        None,
                        lambda: build_track_genre_data(
                            artist, title, lastfm_key=lastfm_key, http=http
                        ),
                    )
                return t, data, anchor, anchor_id, rl, False

            return list(await asyncio.gather(*[_enrich(t) for t in tracks]))

        enriched = asyncio.run(_run_batch())

        # ── Ön-geçiş: her sanatçı için EN İYİ doğrulanmış çapa (ad+id) topla ──────
        # Profil, sanatçının o partideki HERHANGİ track'inden gelen ilk geçerli
        # anchor_id'sinden kurulur — ilk track'in şansına bırakılmaz (2026-07-04,
        # Motive: 50 track id 72196 verirken profil şanssız bir feat-track'in
        # None'ıyla kurulup elektronik alıyordu). Aynı sanatçının bulunan bir
        # track'i profili doğru id'den kurtarır.
        best_anchor: dict[str, tuple[str, int | None]] = {}  # artist → (name, id)
        for t, _td, anchor, anchor_id, _rl, _skipped in enriched:
            artists = t.get("artists") or []
            artist = artists[0] if artists else ""
            if not artist or artist in best_anchor:
                continue
            if anchor and is_artist_match(artist, anchor) and anchor_id:
                best_anchor[artist] = (anchor, anchor_id)

        # ── 4. Boş/kısmi track'ler için artist profili (sanatçı başına 1 kez) ────
        artist_profiles: dict[str, dict] = {}
        # <5 track'li sanatçılar (isim-çakışması eşiği): profil kurulmadı → bu
        # sanatçıların tamamen boş track'leri 'pending' bırakılır (lookup_failed değil).
        small_artists: set[str] = set()

        def _profile_for(
            artist: str, query_name: str | None, query_id: int | None
        ) -> dict:
            if artist not in artist_profiles:
                # Sanatçı-seviyesi en iyi çapa varsa, track'in kendi (belki boş)
                # çapasının yerine onu kullan.
                best = best_anchor.get(artist)
                if best:
                    query_name, query_id = best
                # İsim-çakışması eşiği: <5 track'li sanatçıda İSİM aramalı profil
                # KURMA (Mahmut Tuncer/Yıldız Tilbe koruması). AMA doğrulanmış
                # Deezer artist.id varsa (track çapası: sanatçı+şarkı birlikte
                # eşleşti) id_only profil GÜVENLİ — isim araması hiç yapılmaz,
                # korumanın savunduğu risk o yolda yok (2026-07-16, az-track çözümü).
                if get_db_artist_track_count(artist, client) < _MIN_TRACKS_FOR_PROFILE:
                    if query_id:
                        try:
                            profile = get_or_build_artist_profile(
                                artist, client, http, lastfm_key,
                                query_name=query_name, query_id=query_id,
                                id_only=True,
                            )
                        except RateLimitError:
                            raise
                        except Exception:  # noqa: BLE001
                            logger.warning("Artist id_only profil hatası: %s", artist)
                            profile = {"slots": [], "weights": []}
                        if not profile.get("slots"):
                            # id'li yol da boş → pending kalsın (lookup_failed DEĞİL)
                            small_artists.add(artist)
                        artist_profiles[artist] = profile
                        return artist_profiles[artist]
                    small_artists.add(artist)
                    artist_profiles[artist] = {"slots": [], "weights": []}
                    return artist_profiles[artist]
                try:
                    artist_profiles[artist] = get_or_build_artist_profile(
                        artist, client, http, lastfm_key,
                        query_name=query_name, query_id=query_id,
                    )
                except RateLimitError:
                    # Profil kurulumunda 429 (by_id/isim araması) → YUTMA. Yukarı
                    # taşı ki track lookup_failed yazılmasın, cooldown beslensin,
                    # sonraki tur Deezer soğuyunca doğru çözsün (Motive elektronik bug).
                    raise
                except Exception:  # noqa: BLE001
                    logger.warning("Artist profil hatası: %s", artist)
                    artist_profiles[artist] = {"slots": [], "weights": []}
            return artist_profiles[artist]

        final: list[tuple[str, dict, str]] = []  # (track_id, genre_data, source)
        rate_limited: dict[str, float | None] = {}  # provider → retry_after
        skipped_deadline = 0
        for t, track_data, anchor, anchor_id, rl, skipped in enriched:
            if skipped or track_data is None:
                skipped_deadline += 1
                continue  # bütçe doldu → hiçbir şey yazma, sonraki tur alır
            if rl is not None:
                provider, retry_after = rl
                rate_limited[provider] = retry_after
                continue  # 429 track → lookup_failed YAZMA, sonraki turda dene

            track_id = t["id"]
            artists = t.get("artists") or []
            artist = artists[0] if artists else ""
            track_had_data = bool(track_data["slots"])

            # Bütçe profil aşamasında dolarsa: track'in KENDİ verisi varsa onu yaz
            # (gerçek Deezer verisi; slot doldurma iyileştirmedir, şart değil).
            # Verisi yoksa dokunma — lookup_failed YANLIŞ olur, sonraki tur dener.
            if time.monotonic() > deadline:
                if track_had_data:
                    final.append((track_id, track_data, "multi_source"))
                else:
                    skipped_deadline += 1
                continue

            # Çapa doğrulaması: found_name DB adıyla eşleşiyorsa güvenilir sorgu adı.
            # anchor_id (Deezer artist.id) yalnız isim de doğrulandığında taşınır —
            # yanlış track hit'inin id'sini profile taşımayı önler. (Ön-geçiş zaten
            # sanatçı-seviyesi en iyi çapayı _profile_for içinde önceliklendirir.)
            verified = bool(anchor and is_artist_match(artist, anchor))
            verified_anchor = anchor if verified else None
            verified_id = anchor_id if verified else None

            try:
                if track_had_data:
                    # Kısmi dolu → aile-uyumlu artist türleriyle boş slotları doldur
                    if len(track_data["slots"]) < _TRACK_SLOTS and artist:
                        profile = _profile_for(artist, verified_anchor, verified_id)
                        if profile.get("slots"):
                            track_data = fill_empty_slots(track_data, profile)
                elif artist:
                    # Track tamamen boş → Deezer-taban fallback (isim doğrulanmış)
                    profile = _profile_for(artist, verified_anchor, verified_id)
                    track_data = _build_from_artist_base(profile)
            except RateLimitError as exc:
                # Artist profili 429 → track'i ATLA (lookup_failed YAZMA), cooldown
                # beslenir, sonraki tur çözer. Kısmi dolu track'in mevcut slotları
                # da bu turda yazılmaz — bir sonraki turda profil tamamlanır.
                rate_limited[exc.provider] = exc.retry_after
                continue

            if track_data["slots"]:
                source = "multi_source" if track_had_data else "artist_profile"
                final.append((track_id, track_data, source))
            elif artist in small_artists:
                # Az-track'li sanatçı → 'pending' (kalıcı lookup_failed DEĞİL).
                # Sanatçı track sayısı 5'i geçince olay-tabanlı yeniden denenir.
                final.append((track_id, {}, "pending_small_artist"))
            else:
                final.append((track_id, {}, "lookup_failed"))

    # ── 5. Yazım ─────────────────────────────────────────────────────────────────
    updated = 0
    for track_id, genre_data, source in final:
        if genre_data:
            try:
                client.table("tracks").update({
                    "genres": genre_data["slots"],
                    "genre_data": genre_data,
                    "genre_source": source,
                }).eq("id", track_id).execute()
                updated += 1
            except Exception:  # noqa: BLE001
                logger.warning("genre_data güncelleme başarısız: track=%s", track_id)
        elif source == "pending_small_artist":
            # Az-track'li sanatçı: kalıcı kilit YAZMA. genres/failed_at NULL kalır,
            # genre_pending_reason ile cron'dan geçici olarak çıkarılır. Sanatçı
            # track sayısı ≥5 olunca olay-tabanlı temizlenir (bkz. genre_backfill).
            try:
                client.table("tracks").update(
                    {"genre_pending_reason": "artist_too_small"}
                ).eq("id", track_id).execute()
            except Exception:  # noqa: BLE001
                logger.warning("genre_pending_reason yazılamadı: track=%s", track_id)
        else:
            try:
                client.table("tracks").update(
                    {"genre_lookup_failed_at": "now()", "genre_source": "lookup_failed"}
                ).eq("id", track_id).execute()
            except Exception:  # noqa: BLE001
                logger.warning("genre_lookup_failed_at yazılamadı: track=%s", track_id)

    # ── 429 alınan provider'lar için cooldown damgası (sonraki cron atlar) ──
    for provider, retry_after in rate_limited.items():
        cooldown.set_cooldown(client, provider, retry_after or 60.0, reason="genre_429")

    # ── Otomatik geri kontrol (BÖLÜM 3): bu turda profili DOLU çıkan
    #    sanatçıların lookup_failed track'lerinin işaretini temizle → sonraki
    #    turda FAZ B onları normal akışla doldurur. Ayrı backfill cron'u gerekmez. ──
    _recheck_resolved_artists(client, artist_profiles)

    _clear_genre_pending_if_done(client)

    if skipped_deadline:
        logger.info("Süre bütçesi doldu: %s track sonraki tura bırakıldı", skipped_deadline)
    return {
        "outcome": "success",
        "processed": len(tracks),
        "updated": updated,
        "skipped_deadline": skipped_deadline,
    }


def _reactivate_small_artist_pending(client: Any) -> int:
    """Track sayısı ≥5'e ulaşmış sanatçıların 'pending' track'lerini yeniden aktive et.

    RPC reactivate_small_artist_pending: az-track'li sanatçının track sayısı 5'i
    geçince genre_pending_reason'ı temizler → sonraki batch'te normal işlenir.
    Hata cron'u durdurmamalı (bonus adım). Döner: aktive edilen track sayısı.
    """
    try:
        res = client.rpc("reactivate_small_artist_pending", {}).execute()
        count = res.data if isinstance(res.data, int) else 0
        if count:
            logger.info("Az-track pending yeniden aktive edildi: %s track", count)
        return count or 0
    except Exception:  # noqa: BLE001
        logger.warning("reactivate_small_artist_pending RPC başarısız (atlandı)")
        return 0


def _recheck_resolved_artists(client: Any, artist_profiles: dict[str, dict]) -> int:
    """Otomatik geri kontrol: bu turda profili DOLU çıkan sanatçıların
    lookup_failed track'lerinin işaretini temizle (BÖLÜM 3, 2026-07-03).

    Yalnızca bu cron turunda inşa edilen ve slots'u DOLU olan sanatçılar için
    çalışır (muhafazakar kapsam → sonsuz döngü riski yok: profili boş kalan
    sanatçının track'lerine dokunulmaz, çözülmüş sanatçı bir daha "yeni" olmaz).

    Döner: işareti temizlenen sanatçı sayısı (track sayısı değil — tek UPDATE/sanatçı).
    """
    cleared = 0
    for artist, profile in artist_profiles.items():
        if not (profile.get("slots") or []):
            continue  # profil boş → dokunma (döngü koruması)
        try:
            (
                client.table("tracks")
                .update({"genre_lookup_failed_at": None, "genre_source": None})
                .contains("artists", [artist])
                .not_.is_("genre_lookup_failed_at", "null")
                .execute()
            )
            cleared += 1
        except Exception:  # noqa: BLE001
            logger.warning("Geri kontrol temizleme başarısız: %s", artist)
    return cleared


def _clear_genre_pending_if_done(client: Any) -> None:
    """genres IS NULL track kalmadıysa tüm genre_pending export_jobs'u temizle.

    tracks global katalog olduğundan kullanıcı bazlı eşleme gerekmez; idempotent.
    """
    try:
        res = (
            client.table("tracks").select("id").is_("genres", "null").limit(1).execute()
        )
        if not (res.data or []):
            client.table("export_jobs").update({"genre_pending": False}).eq(
                "genre_pending", True
            ).execute()
            logger.info("Tüm genre'ler tamamlandı → genre_pending temizlendi")
    except Exception:  # noqa: BLE001
        logger.warning("genre_pending temizleme başarısız")

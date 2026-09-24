"""ISRC dolgusu — Deezer öncelikli (Fable 5 yeniden tasarımı, 2026-07-20).

── NEDEN Deezer (canlı ölçüm, 80 track örneklem) ──

| Kaynak | Bilinen ISRC'de doğruluk | Bilinmeyen katalogda isabet |
|---|---|---|
| **Deezer** | 40/40 doğru şarkı (34 birebir ISRC + 6 aynı şarkının farklı basımı, YANLIŞ ŞARKI: 0) | **37/40 (%92,5)** |
| MusicBrainz | 15/40 birebir | 8/40 (%20) |
| iTunes | ISRC alanı hiç yok | — |
| Spotify tekil uç | birebir ama **6,4 saat ceza sicili** (2026-07-12) | — |

Deezer artı değerleri: API key YOK, kota cezası sicili YOK (limit 50 istek/5sn,
canlı ölçüm 2026-07-16), aynı yanıtta duration + album + release_date de geliyor.
"Farklı basım" ISRC'leri (remaster/derleme) yanlış EŞLEŞME değildir — kayıt ailesi
aynıdır; eşleşme motoru ISRC tutmazsa zaten fuzzy'ye düşer (bugünkü durum).

── Güvenlik ağları (CLAUDE.md §1.6) ──
- Tüm istekler deezer_genre._paced_get geçidinden: süreç geneli ≥250ms aralık.
- Kota (429 veya gövde code=4/700) → RateLimitError → cooldown DB'ye yazılır,
  tur `partial` biter. Tur başında is_blocked kontrolü — bloklu isek SIFIR istek.
- Bulunamayan track de RPC'ye null satır olarak gider → catalog_backfill_at
  damgalanır → sonsuz retry YOK.
- Yazım COALESCE'li mevcut `apply_catalog_backfill` RPC'si ile → veri SİLİNMEZ,
  migration gerekmez.
- Zaman bütçesi: tur `time_budget_s`'yi aşınca kalanı sonraki cron'a bırakır.
"""
from __future__ import annotations

import logging
import re
import time
import urllib.parse
from typing import Any

from app.services import cooldown
from app.services.deezer_genre import _paced_get, _raise_if_quota, _similar, fold
from app.services.genre_errors import RateLimitError

logger = logging.getLogger("rosso.worker.isrc_backfill")

_SEARCH_URL = "https://api.deezer.com/search"
_TRACK_URL = "https://api.deezer.com/track/{id}"
_PROVIDER = "deezer"

# Doğrulama eşikleri — deezer_genre Aşama B ile aynı (canlı kalibrasyon 2026-07-03).
# Ölçümde bu eşiklerle yanlış şarkı kabulü 0/80 idi.
_ARTIST_MATCH_MIN = 0.85
_TITLE_MATCH_MIN = 0.60

# Kuyruk sorgusu tur başına bu kadar track çeker; RPC yazımı da aynı boyutta.
_BATCH = 50

# 429'da Retry-After başlığı yoksa varsayılan bekleme. Deezer penceresi 5 sn ama
# kota fırtınasında kısa bekleme cezayı besler (Spotify dersi) — temkinli 10 dk.
_DEFAULT_COOLDOWN_S = 600.0


# ── Süsleme temizliği (FALLBACK — Efendim kararı, 2026-07-21) ───────────────
#
# Canlı sonda (60 örneklem) bulundu: bulunamayanların **%18'i** aslında
# Deezer'da VAR; sanatçı skoru 1.00 tutuyor ama başlıktaki süsleme benzerliği
# eşiğin altına düşürüyor ve DOĞRU şarkı reddediliyor:
#
#   'Gasoline (feat. Taylor Swift)'  ↔ 'Gasoline'   → 0.43  (eşik 0.60)
#   'Lucky You (feat. Joyner Lucas)' ↔ 'Lucky You'  → 0.46
#
# ⚠ Efendim'in kararı (ve doğrusu): bu temizlik **doğrudan** uygulanmaz,
# yalnızca FALLBACK'tir. Sebep: süslemeyi baştan atmak ayrı kayıtları
# birbirine karıştırır — remix ile orijinali, farklı misafirli sürümleri
# aynı şey sanabiliriz. Yani önce TAM başlıkla dene; ancak hiçbir aday
# geçemezse süslemesiz kıyasa düş.
#
# Eşikler DEĞİŞMEDİ. Eşiği düşürmek yanlış çözüm olurdu: 0.40'a çekmek
# 'BARRI' → 'BRING IT BACK!' (0.21) sınıfı yanlış eşleşmeleri de içeri alırdı.
_DECOR_RE = re.compile(
    r"""
      \s*[\(\[\{]                      # açılış parantezi
        [^\)\]\}]*                     # içerik
        \b(?:feat|ft|with|remix|mix|edit|version|remaster|remastered
           |from|orijinal|original|karaoke|instrumental|cover|live|acoustic
           |sped\s*up|speed\s*up|slowed)\b
        [^\)\]\}]*
      [\)\]\}]
      |                                 # YA DA tire ile ayrılmış kuyruk ekleri
      \s+-\s+
        (?:[^-]*\b(?:remix|mix|edit|version|remaster|remastered|live
                    |sped\s*up|speed\s*up|slowed|acoustic|instrumental)\b[^-]*)
      \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


def strip_decorations(title: str) -> str:
    """Başlıktan misafir/sürüm süslemesini atar. Boşalırsa orijinali korur.

    'Gasoline (feat. Taylor Swift)'            → 'Gasoline'
    'MUTT (feat. Chris Brown) [CB REMIX]'      → 'MUTT'
    'Sen İstanbulsun - Speed Up'               → 'Sen İstanbulsun'
    'Ay (Original Mix)'                        → 'Ay'

    Süsleme İÇERMEYEN başlık aynen döner — o durumda fallback zaten
    ilk denemeyle aynı olur ve boşuna istek atılmaz (bkz. lookup_isrc).
    """
    if not title:
        return title
    prev = None
    out = title
    # Birden çok blok olabilir ('… (feat. X) [CB REMIX]') — sabit noktaya kadar.
    while prev != out:
        prev = out
        out = _DECOR_RE.sub("", out).strip()
    return out or title


def _release_year(release_date: str | None) -> int | None:
    """Deezer release_date ('YYYY-MM-DD' | 'YYYY') → yıl. Saçma değer → None."""
    if not release_date or not isinstance(release_date, str):
        return None
    head = release_date[:4]
    if not head.isdigit():
        return None
    year = int(head)
    return year if 1900 <= year <= 2100 else None


def parse_deezer_track(t: dict[str, Any] | None) -> dict[str, Any] | None:
    """Deezer /track/{id} yanıtı → dolgu satırı (spotify_id çağıran ekler).

    Deezer süreyi SANİYE döndürür → ms'e çevrilir. ISRC boş string gelirse
    None (RPC nullif zaten korur ama satırı temiz tutalım).
    """
    if not t or not t.get("id"):
        return None
    duration_s = t.get("duration")
    return {
        "isrc": (t.get("isrc") or "").strip().upper() or None,
        "duration_ms": duration_s * 1000 if isinstance(duration_s, int) and duration_s > 0 else None,
        "album": (t.get("album") or {}).get("title"),
        "release_year": _release_year(t.get("release_date")),
    }


def _search(query: str, http: Any) -> list[dict[str, Any]]:
    """Deezer /search → aday listesi (5). Kota → RateLimitError."""
    import httpx

    q = urllib.parse.quote(query)
    try:
        resp = _paced_get(http, f"{_SEARCH_URL}?q={q}&limit=5")
        resp.raise_for_status()
        payload = resp.json()
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code == 429:
            ra = exc.response.headers.get("Retry-After")
            raise RateLimitError(_PROVIDER, float(ra) if ra else None) from exc
        logger.warning("Deezer search başarısız: %s", query)
        return []
    except Exception:  # noqa: BLE001
        logger.warning("Deezer search başarısız: %s", query)
        return []
    _raise_if_quota(payload)
    return payload.get("data", []) if isinstance(payload, dict) else []


def _pick_best(
    candidates: list[dict[str, Any]], artist: str, title: str
) -> dict[str, Any] | None:
    """İlk 5 adaydan sanatçı+başlık doğrulamasını geçen EN benzerini seç.

    Doğrulama zorunlu — Deezer ilk sonucu bazen alakasız (Stabil→Ati242 sınıfı).
    Geçen yoksa None: yanlış şarkının ISRC'sini yazmaktansa hiç yazmamak iyidir.
    """
    best: tuple[float, dict[str, Any]] | None = None
    for hit in candidates[:5]:
        found_artist = (hit.get("artist") or {}).get("name") or ""
        found_title = hit.get("title") or hit.get("title_short") or ""
        sa = _similar(artist, found_artist)
        st = _similar(title, found_title)
        if sa < _ARTIST_MATCH_MIN or st < _TITLE_MATCH_MIN:
            continue
        score = sa + st
        if best is None or score > best[0]:
            best = (score, hit)
    return best[1] if best else None


def lookup_track_payload(artist: str, title: str, http: Any) -> dict[str, Any] | None:
    """artist + title → Deezer'ın HAM /track/{id} yanıtı | None (doğrulanmış eşleşme).

    (2026-09-24) `lookup_isrc` bunun üstüne kuruldu; `deezer_cover_runner` de aynı
    doğrulanmış eşleşmeyi kullanıp kapak (`album.cover_big`) alır — eşleşme mantığı
    TEK yerde, iki kullanım.

    İki aşama (deezer_genre ile aynı desen, canlı kanıtlı):
      A) fold'lu kesin sorgu — İ/aksan sorununu çözer
      B) düz arama — A boş kalırsa
    Her iki aşamada da _pick_best doğrulaması zorunlu. Hit → /track/{id}
    (arama sonucunda ISRC YOK; tekil track ucunda var — canlı ölçüm).
    """
    if not artist or not title:
        return None
    # Tamamen sayısal başlık Deezer arama ayrıştırıcısını kırıyor (canlı, 2026-07-02).
    if title.strip().isdigit():
        return None

    hit = _pick_best(
        _search(f'artist:"{fold(artist)}" track:"{fold(title)}"', http), artist, title
    )
    if hit is None:
        hit = _pick_best(_search(f"{artist} {title}", http), artist, title)

    # Aşama C — SÜSLEMESİZ FALLBACK (yalnız A ve B başarısızsa).
    # Tam başlık her zaman önce denenir; bu dal ancak hiçbir aday geçemezse
    # çalışır, yani remix/farklı-sürüm ayrımını bozmaz.
    if hit is None:
        bare = strip_decorations(title)
        # Süsleme yoksa bare == title olur; aynı sorguyu tekrar atmanın anlamı
        # yok (boşuna istek = boşuna rate-limit tüketimi, §1.6).
        if bare and bare.casefold() != title.casefold():
            hit = _pick_best(
                _search(f'artist:"{fold(artist)}" track:"{fold(bare)}"', http),
                artist,
                bare,  # ⚠ kıyas da SÜSLEMESİZ başlıkla yapılmalı, yoksa
            )          # aday yine 0.43'te kalır ve fallback hiçbir şey değiştirmez

    if hit is None or not hit.get("id"):
        return None

    import httpx

    try:
        resp = _paced_get(http, _TRACK_URL.format(id=hit["id"]))
        resp.raise_for_status()
        payload = resp.json()
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code == 429:
            ra = exc.response.headers.get("Retry-After")
            raise RateLimitError(_PROVIDER, float(ra) if ra else None) from exc
        logger.warning("Deezer track başarısız: id=%s", hit["id"])
        return None
    except Exception:  # noqa: BLE001
        logger.warning("Deezer track başarısız: id=%s", hit["id"])
        return None
    _raise_if_quota(payload)
    return payload if isinstance(payload, dict) else None


def lookup_isrc(artist: str, title: str, http: Any) -> dict[str, Any] | None:
    """artist + title → Deezer'dan dolgu satırı (isrc/duration/album/yıl) | None."""
    payload = lookup_track_payload(artist, title, http)
    return parse_deezer_track(payload) if payload else None


def _null_row(spotify_id: str) -> dict[str, Any]:
    """Bulunamayan track satırı — RPC catalog_backfill_at damgalar, retry biter."""
    return {
        "spotify_id": spotify_id,
        "isrc": None, "duration_ms": None, "album": None, "release_year": None,
    }


def run_one_isrc_batch(
    client: Any, *, max_batches: int = 8, time_budget_s: float = 240.0
) -> dict[str, Any]:
    """Bir cron turunda en çok `max_batches` × 50 track'i Deezer'dan dolgula.

    Track başına ~2-3 istek, ≥250ms aralık → 50'lik parti ~30-40 sn.
    8 parti ≈ 400 track ≈ 4-5 dk; bütçe aşılırsa kalan sonraki tura devreder.
    28k'lık kuyruk 10 dk'lık cron'la ~3 günde biter — yavaş ama cezasız (şart 1).
    """
    import httpx

    started = time.monotonic()
    processed = 0
    updated = 0
    found = 0

    # Devre kesici: Deezer kota fırtınasındaysak HİÇ istek atma (Spotify dersi).
    blocked, remaining = cooldown.is_blocked(client, _PROVIDER)
    if blocked:
        logger.warning(
            "Deezer cooldown aktif (%d sn) — ISRC dolgusu atlanıyor", remaining
        )
        return {
            "outcome": "blocked",
            "processed": 0, "updated": 0, "found": 0,
            "cooldown_remaining_s": remaining,
        }

    with httpx.Client(timeout=httpx.Timeout(15.0)) as http:
        for _ in range(max_batches):
            if time.monotonic() - started > time_budget_s:
                # Bütçe doldu — kalan iş sonraki cron'un. Bu hata değil.
                break

            res = (
                client.table("tracks")
                .select("spotify_id,title,artists")
                .is_("isrc", "null")
                .is_("catalog_backfill_at", "null")
                .not_.is_("spotify_id", "null")
                .order("created_at")
                .limit(_BATCH)
                .execute()
            )
            batch = [r for r in (res.data or []) if r.get("spotify_id")]
            if not batch:
                return {
                    "outcome": "empty" if processed == 0 else "success",
                    "processed": processed, "updated": updated, "found": found,
                }

            rows: list[dict[str, Any]] = []
            try:
                for tr in batch:
                    artists = tr.get("artists") or []
                    artist = artists[0] if artists else ""
                    row = lookup_isrc(artist, tr.get("title") or "", http)
                    if row:
                        row["spotify_id"] = tr["spotify_id"]
                        rows.append(row)
                        if row.get("isrc"):
                            found += 1
                    else:
                        rows.append(_null_row(tr["spotify_id"]))
            except RateLimitError as exc:
                # Kota: o ana kadar TAMAMLANAN track'ler yazılır, tur biter.
                # Yarım kalan parti damgalanmaz → sonraki tur kaldığı yerden.
                used = cooldown.set_cooldown(
                    client, _PROVIDER,
                    exc.retry_after or _DEFAULT_COOLDOWN_S,
                    reason="isrc_backfill_429",
                )
                if rows:
                    try:
                        w = client.rpc(
                            "apply_catalog_backfill", {"p_rows": rows}
                        ).execute()
                        updated += int(w.data or 0)
                        processed += len(rows)
                    except Exception:  # noqa: BLE001
                        logger.exception("apply_catalog_backfill yazım hatası (kota sonrası)")
                logger.warning(
                    "Deezer kotası — %d sn cooldown yazıldı, tur kapandı", used
                )
                return {
                    "outcome": "partial",
                    "processed": processed, "updated": updated, "found": found,
                    "error": "rate_limited", "cooldown_s": used,
                }

            try:
                w = client.rpc("apply_catalog_backfill", {"p_rows": rows}).execute()
                updated += int(w.data or 0)
            except Exception as exc:  # noqa: BLE001
                # Yazım hatası SESSİZCE yutulmaz (B19 dersi).
                logger.exception("apply_catalog_backfill yazım hatası")
                return {
                    "outcome": "error",
                    "processed": processed, "updated": updated, "found": found,
                    "error": str(exc)[:300],
                }

            processed += len(batch)

    return {
        "outcome": "success",
        "processed": processed, "updated": updated, "found": found,
    }

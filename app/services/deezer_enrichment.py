"""Genre enrichment servisi — Deezer primary, MusicBrainz + Last.fm fallback.

Mimari (2026-06-24 araştırma kararı):
  Katman 1: Deezer  — api.deezer.com/2.0/track/isrc:<ISRC>
            API key yok, 10 req/sn, resmi genre_id döner.
  Katman 2: MusicBrainz — musicbrainz.org/ws/2/recording?query=isrc:...
            1 req/sn limit, crowdsourced. Artist+title meta verisini
            de çeker → Last.fm'e aktarır.
  Katman 3: Last.fm — artist.getInfo(artist) → toptags
            ISRC desteklemiyor; MusicBrainz'den gelen artist adıyla çalışır.
            Serbest tag'ler → GENRE_MAP sözlüğüyle kanonik türe map'lenir.

Supabase tracks tablosuna yalnızca `genres` alanı yazılır; diğer alanlar
(isrc, title, artists) zaten Spotify enrichment veya upsert_track tarafından
doldurulmuştur.

Apple Music: kaldırıldı (lisans kısıtı + $99/yıl maliyet, bkz. araştırma raporu).
Spotify genre: Dev Mode kısıtları nedeniyle burada kullanılmıyor.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger("rosso.worker.deezer_enrichment")

# ──────────────────────────────────────────────────────────────────────────────
# Rate limit ayarları (env ile override edilebilir)
# ──────────────────────────────────────────────────────────────────────────────
_DEEZER_DELAY_S      = float(os.environ.get("DEEZER_DELAY_S", "0.12"))       # ~8 req/sn (limit 10)
_MB_DELAY_S          = float(os.environ.get("MB_DELAY_S", "1.1"))             # 1 req/sn limit
_LASTFM_DELAY_S      = float(os.environ.get("LASTFM_DELAY_S", "0.25"))        # ~4 req/sn (limit yok ama nazik)
_LASTFM_MIN_COUNT    = int(os.environ.get("LASTFM_MIN_TAG_COUNT", "20"))      # gürültü filtresi
_MB_USER_AGENT       = os.environ.get(
    "MB_USER_AGENT",
    "Rosso/1.0 (muhmmt.msn@gmail.com)",  # MusicBrainz politikası: açıklayıcı UA zorunlu
)

# ──────────────────────────────────────────────────────────────────────────────
# Kanonik tür sözlüğü (Last.fm serbest tag → standart tür)
# Araştırma sonucu: sözlük seed kaynakları Every Noise + Discogs Style.
# Kullanıcı araştırması tamamlandığında genişletilecek.
# ──────────────────────────────────────────────────────────────────────────────
GENRE_MAP: dict[str, str] = {
    # ── Pop ──────────────────────────────────────────────────────────────────
    "pop": "Pop", "turkish pop": "Pop", "türk pop": "Pop", "dance pop": "Pop",
    "electropop": "Pop", "synth-pop": "Synth-Pop", "indie pop": "Indie Pop",
    "art pop": "Art Pop", "k-pop": "K-Pop", "j-pop": "J-Pop",
    "pop rock": "Pop Rock", "dream pop": "Dream Pop", "chamber pop": "Chamber Pop",

    # ── Rock ──────────────────────────────────────────────────────────────────
    "rock": "Rock", "turkish rock": "Turkish Rock", "türk rock": "Turkish Rock",
    "alternative rock": "Alternative Rock", "indie rock": "Indie Rock",
    "classic rock": "Classic Rock", "hard rock": "Hard Rock",
    "punk rock": "Punk Rock", "punk": "Punk", "post-punk": "Post-Punk",
    "progressive rock": "Progressive Rock", "psychedelic rock": "Psychedelic Rock",
    "grunge": "Grunge", "shoegaze": "Shoegaze", "math rock": "Math Rock",
    "post-rock": "Post-Rock", "garage rock": "Garage Rock",
    "new wave": "New Wave", "art rock": "Art Rock",

    # ── Metal ─────────────────────────────────────────────────────────────────
    "metal": "Metal", "heavy metal": "Metal", "death metal": "Death Metal",
    "black metal": "Black Metal", "doom metal": "Doom Metal",
    "thrash metal": "Thrash Metal", "power metal": "Power Metal",
    "symphonic metal": "Symphonic Metal", "folk metal": "Folk Metal",
    "nu-metal": "Nu-Metal",

    # ── Hip-Hop / Rap ─────────────────────────────────────────────────────────
    "hip hop": "Hip-Hop", "hip-hop": "Hip-Hop", "rap": "Hip-Hop",
    "turkish hip hop": "Turkish Hip-Hop", "turkish rap": "Turkish Hip-Hop",
    "türkçe rap": "Turkish Hip-Hop", "tr rap": "Turkish Hip-Hop",
    "trap": "Trap", "drill": "Drill", "boom bap": "Boom Bap",
    "conscious hip hop": "Hip-Hop", "gangsta rap": "Hip-Hop",
    "lo-fi hip hop": "Lo-Fi", "lo-fi": "Lo-Fi",

    # ── Electronic / Dance ────────────────────────────────────────────────────
    "electronic": "Electronic", "electronica": "Electronic",
    "edm": "EDM", "dance": "Dance", "house": "House",
    "deep house": "Deep House", "tech house": "Tech House",
    "techno": "Techno", "trance": "Trance", "progressive trance": "Trance",
    "drum and bass": "Drum & Bass", "dnb": "Drum & Bass",
    "dubstep": "Dubstep", "ambient": "Ambient",
    "chillwave": "Chillwave", "chillout": "Chillout",
    "downtempo": "Downtempo", "trip-hop": "Trip-Hop",
    "idm": "IDM", "experimental electronic": "Experimental",
    "breakbeat": "Breakbeat", "garage": "UK Garage",
    "future bass": "Future Bass", "bass music": "Bass Music",

    # ── R&B / Soul / Funk ─────────────────────────────────────────────────────
    "r&b": "R&B", "rnb": "R&B", "contemporary r&b": "R&B",
    "soul": "Soul", "neo soul": "Neo Soul", "funk": "Funk",
    "gospel": "Gospel", "motown": "Soul",

    # ── Jazz / Blues ──────────────────────────────────────────────────────────
    "jazz": "Jazz", "nu jazz": "Jazz", "jazz fusion": "Jazz Fusion",
    "blues": "Blues", "electric blues": "Blues", "soul blues": "Blues",

    # ── Classical / Orchestral ────────────────────────────────────────────────
    "classical": "Classical", "contemporary classical": "Classical",
    "orchestral": "Orchestral", "opera": "Opera", "film score": "Soundtrack",
    "soundtrack": "Soundtrack", "score": "Soundtrack",

    # ── Folk / Country / Acoustic ─────────────────────────────────────────────
    "folk": "Folk", "indie folk": "Indie Folk", "folk rock": "Folk Rock",
    "country": "Country", "americana": "Americana",
    "singer-songwriter": "Singer-Songwriter", "acoustic": "Acoustic",

    # ── Turkish / Regional ────────────────────────────────────────────────────
    "turkish": "Turkish Music", "türkçe": "Turkish Music",
    "türkü": "Türk Halk Müziği", "halk müziği": "Türk Halk Müziği",
    "turkish folk": "Türk Halk Müziği", "türk halk müziği": "Türk Halk Müziği",
    "thm": "Türk Halk Müziği",
    "anadolu rock": "Anadolu Rock", "anatolian rock": "Anadolu Rock",
    "arabesque": "Arabesk", "arabesk": "Arabesk", "fantezi": "Arabesk",
    "ottoman": "Klasik Türk Müziği", "ottoman classical": "Klasik Türk Müziği",
    "turkish classical": "Klasik Türk Müziği", "türk sanat müziği": "Klasik Türk Müziği",
    "türk klasik müziği": "Klasik Türk Müziği", "klasik türk müziği": "Klasik Türk Müziği",
    "fasıl": "Klasik Türk Müziği",

    # ── World / Reggae / Latin ────────────────────────────────────────────────
    "world": "World Music", "world music": "World Music",
    "reggae": "Reggae", "dancehall": "Dancehall",
    "latin": "Latin", "latin pop": "Latin Pop",
    "bossa nova": "Bossa Nova", "flamenco": "Flamenco",
    "afrobeat": "Afrobeat", "cumbia": "Latin",

    # ── Indie / Alternative ───────────────────────────────────────────────────
    "indie": "Indie", "alternative": "Alternative",
    "emo": "Emo", "emo pop": "Emo", "post-hardcore": "Post-Hardcore",
    "screamo": "Screamo",

    # ── Diğer / Genel ────────────────────────────────────────────────────────
    "experimental": "Experimental", "noise": "Experimental",
    "avant-garde": "Experimental", "abstract": "Experimental",
    "meditation": "Ambient", "new age": "New Age",
    "christmas": "Holiday", "holiday": "Holiday",

    # ── Deezer'a özgü tür adları (TR/lokalize) ────────────────────────────────
    # Deezer genre listesi kendi taksonomisini kullanır ve bölgeye göre
    # lokalize gelir. Bunları kanonik türlere map'le (2026-06-25 bulgusu).
    "alternatif": "Alternative",
    "rap/hip hop": "Hip-Hop", "rap/hiphop": "Hip-Hop",
    "films/games": "Soundtrack", "film/tv": "Soundtrack",
    "musiques du monde": "World Music",
    "dance": "Dance",
    "musique turque": "Turkish Music",
    "türkçe pop": "Pop", "türkçe rap": "Turkish Hip-Hop",
    "arabe": "Arabesk",
}


def _map_tags_to_genres(tags: list[dict[str, Any]]) -> list[str]:
    """Last.fm toptags listesini kanonik türlere dönüştür.

    - count >= _LASTFM_MIN_COUNT gürültü filtresi
    - GENRE_MAP'ten eşleşen tag'ler alınır, lowercase normalize ile
    - Duplicate'ler çıkarılır, sıra korunur
    """
    seen: set[str] = set()
    result: list[str] = []
    for tag in tags:
        count = int(tag.get("count", 0))
        if count < _LASTFM_MIN_COUNT:
            continue
        name = tag.get("name", "").strip().lower()
        canonical = GENRE_MAP.get(name)
        if canonical and canonical not in seen:
            seen.add(canonical)
            result.append(canonical)
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Katman 1: Deezer
# ──────────────────────────────────────────────────────────────────────────────

def _deezer_genres_by_isrc(isrc: str, http: Any) -> list[str] | None:
    """Deezer'dan ISRC ile tür listesi çek.

    İKİ ÇAĞRI gerekir (2026-06-25 bulgusu):
      1. track/isrc:<ISRC> → yanıtta album.id var ama album.genres YOK
         (track endpoint genre listesini nested vermez, sadece album.id).
      2. album/<album_id> → genres.data[] burada gelir.

    Döner: genre adları listesi (boş liste = eşleşme var ama genre yok),
           None = track bulunamadı / hata.
    """
    try:
        resp = http.get(
            f"https://api.deezer.com/2.0/track/isrc:{isrc}",
            timeout=8,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()

        if data.get("error"):
            return None

        album = data.get("album") or {}
        album_id = album.get("id")
        if not album_id:
            return []

        # 2. çağrı: album genre'si album endpoint'inden gelir
        time.sleep(_DEEZER_DELAY_S)
        album_resp = http.get(
            f"https://api.deezer.com/2.0/album/{album_id}",
            timeout=8,
        )
        if album_resp.status_code == 404:
            return []
        album_resp.raise_for_status()
        album_data = album_resp.json()
        if album_data.get("error"):
            return []

        genre_data = album_data.get("genres") or {}
        genres: list[str] = []
        for g in genre_data.get("data", []):
            name = g.get("name", "").strip()
            if name:
                # Deezer'ın kendi genre adlarını GENRE_MAP üzerinden normalize et
                canonical = GENRE_MAP.get(name.lower(), name)
                if canonical not in genres:
                    genres.append(canonical)

        return genres
    except Exception:  # noqa: BLE001
        logger.warning("Deezer sorgu hatası: ISRC=%s", isrc)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Katman 2: MusicBrainz
# ──────────────────────────────────────────────────────────────────────────────

def _musicbrainz_lookup(isrc: str, http: Any) -> dict[str, Any] | None:
    """MusicBrainz'den ISRC → artist + title + MBID çek.

    Döner: {"artist": str, "title": str, "mbid": str} veya None.
    Genres doğrudan MB'den alınmıyor çünkü TR/niş sanatçılarda genelde boş;
    sonuç Last.fm'e aktarılır.
    """
    headers = {"User-Agent": _MB_USER_AGENT, "Accept": "application/json"}
    try:
        # Adım 1: ISRC → kayıt arama
        resp = http.get(
            "https://musicbrainz.org/ws/2/recording",
            params={"query": f"isrc:{isrc}", "fmt": "json", "limit": 1},
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        recordings = resp.json().get("recordings", [])
        if not recordings:
            return None

        rec = recordings[0]
        mbid = rec.get("id")
        title = rec.get("title", "")
        artists = rec.get("artist-credit", [])
        artist = artists[0].get("artist", {}).get("name", "") if artists else ""

        if not artist or not title:
            return None

        return {"artist": artist, "title": title, "mbid": mbid}
    except Exception:  # noqa: BLE001
        logger.warning("MusicBrainz sorgu hatası: ISRC=%s", isrc)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Katman 3: Last.fm
# ──────────────────────────────────────────────────────────────────────────────

def _lastfm_genres(artist: str, title: str, api_key: str, http: Any) -> list[str]:
    """Last.fm artist.getInfo → toptags → kanonik türler.

    track.getInfo yerine artist.getInfo tercih edilir: ~%93 coverage vs ~%40.
    tag count < _LASTFM_MIN_COUNT filtresi gürültüyü eler.
    """
    try:
        resp = http.get(
            "https://ws.audioscrobbler.com/2.0/",
            params={
                "method": "artist.getInfo",
                "artist": artist,
                "api_key": api_key,
                "format": "json",
                "autocorrect": 1,
            },
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
        tags = (
            data.get("artist", {})
            .get("tags", {})
            .get("tag", [])
        )
        genres = _map_tags_to_genres(tags)
        if genres:
            return genres

        # Artist tags boşsa track.getInfo dene
        time.sleep(_LASTFM_DELAY_S)
        resp2 = http.get(
            "https://ws.audioscrobbler.com/2.0/",
            params={
                "method": "track.getInfo",
                "artist": artist,
                "track": title,
                "api_key": api_key,
                "format": "json",
                "autocorrect": 1,
            },
            timeout=8,
        )
        resp2.raise_for_status()
        data2 = resp2.json()
        tags2 = (
            data2.get("track", {})
            .get("toptags", {})
            .get("tag", [])
        )
        return _map_tags_to_genres(tags2)
    except Exception:  # noqa: BLE001
        logger.warning("Last.fm sorgu hatası: artist=%s", artist)
        return []


# ──────────────────────────────────────────────────────────────────────────────
# Ana enrichment fonksiyonu
# ──────────────────────────────────────────────────────────────────────────────

def enrich_track_genres(
    isrc: str,
    http: Any,
    lastfm_api_key: str = "",
    *,
    artist_hint: str = "",
    title_hint: str = "",
) -> list[str]:
    """ISRC için Deezer→MusicBrainz→Last.fm üç katmanlı genre enrichment.

    artist_hint / title_hint: Deezer ve MB başarısız olursa Last.fm'e
    doğrudan geçmek için track'in bilinen artist/title'ı (Spotify'dan).

    Döner: kanonik genre listesi (boş = zenginleştirme başarısız).
    """
    # Katman 1: Deezer
    deezer_result = _deezer_genres_by_isrc(isrc, http)
    time.sleep(_DEEZER_DELAY_S)

    if deezer_result is not None and len(deezer_result) > 0:
        logger.debug("Deezer genre bulundu: ISRC=%s genres=%s", isrc, deezer_result)
        return deezer_result

    # Katman 2: MusicBrainz (artist+title almak için)
    time.sleep(_MB_DELAY_S)
    mb_result = _musicbrainz_lookup(isrc, http)

    artist = (mb_result or {}).get("artist", "") or artist_hint
    title  = (mb_result or {}).get("title",  "") or title_hint

    if not artist:
        logger.debug("Genre zenginleştirme başarısız (artist yok): ISRC=%s", isrc)
        return []

    if not lastfm_api_key:
        logger.debug("Last.fm API key yok, enrichment tamamlanamadı: ISRC=%s", isrc)
        return []

    # Katman 3: Last.fm
    time.sleep(_LASTFM_DELAY_S)
    genres = _lastfm_genres(artist, title, lastfm_api_key, http)
    if genres:
        logger.debug("Last.fm genre bulundu: ISRC=%s artist=%s genres=%s", isrc, artist, genres)
    else:
        logger.debug("Genre zenginleştirme tüm katmanlarda başarısız: ISRC=%s", isrc)

    return genres


# ──────────────────────────────────────────────────────────────────────────────
# Toplu enrichment — tracks tablosundaki ISRC'siz/genresiz kayıtları zenginleştir
# ──────────────────────────────────────────────────────────────────────────────

def enrich_track_genres_no_isrc(
    artist: str,
    title: str,
    http: Any,
    lastfm_api_key: str = "",
) -> list[str]:
    """ISRC olmayan track'ler için doğrudan Last.fm → artist+title ile genre çek.

    MusicBrainz'i atlar (ISRC gerektiriyor); sadece Last.fm artist.getInfo kullanır.
    Döner: kanonik genre listesi (boş = başarısız).
    """
    if not artist or not lastfm_api_key:
        return []
    return _lastfm_genres(artist, title, lastfm_api_key, http)


def run_genre_enrichment_batch(
    supabase_client: Any,
    http: Any,
    lastfm_api_key: str = "",
    *,
    batch_limit: int = 500,
    include_isrc_less: bool = True,
) -> dict[str, int]:
    """tracks tablosundaki genre'siz kayıtları toplu zenginleştir.

    İki grup işlenir:
      1. ISRC'li tracks: Deezer→MusicBrainz→Last.fm zinciri (tam 3 katman)
      2. ISRC'siz tracks (include_isrc_less=True): doğrudan Last.fm artist+title

    Döner: {"processed": int, "enriched": int, "failed": int,
            "isrc_enriched": int, "no_isrc_enriched": int}
    """
    processed = enriched = failed = 0
    isrc_enriched = no_isrc_enriched = 0

    # ── Grup 1: ISRC'li, genre'siz ────────────────────────────────────────────
    try:
        res = (
            supabase_client.table("tracks")
            .select("id, isrc, title, artists")
            .is_("genres", "null")
            .not_.is_("isrc", "null")
            .limit(batch_limit)
            .execute()
        )
        isrc_tracks = res.data or []
    except Exception:
        logger.exception("tracks (ISRC'li) sorgusu başarısız")
        isrc_tracks = []

    logger.info("Genre enrichment (ISRC'li) başlıyor: %d track", len(isrc_tracks))

    for track in isrc_tracks:
        processed += 1
        isrc = track.get("isrc")
        if not isrc:
            continue

        artist_hint = (track.get("artists") or [""])[0]
        title_hint  = track.get("title", "")

        genres = enrich_track_genres(
            isrc, http, lastfm_api_key,
            artist_hint=artist_hint,
            title_hint=title_hint,
        )

        if genres:
            try:
                supabase_client.table("tracks").update(
                    {"genres": genres}
                ).eq("id", track["id"]).execute()
                enriched += 1
                isrc_enriched += 1
            except Exception:
                logger.warning("tracks.genres yazılamadı: id=%s", track["id"])
                failed += 1
        else:
            failed += 1

    # ── Grup 2: ISRC'siz, genre'siz ───────────────────────────────────────────
    if include_isrc_less and lastfm_api_key:
        no_isrc_limit = max(50, batch_limit // 5)  # fazla quota yememek için sınırlı
        try:
            res2 = (
                supabase_client.table("tracks")
                .select("id, title, artists")
                .is_("genres", "null")
                .is_("isrc", "null")
                .not_.is_("title", "null")
                .not_.is_("artists", "null")
                .limit(no_isrc_limit)
                .execute()
            )
            no_isrc_tracks = res2.data or []
        except Exception:
            logger.exception("tracks (ISRC'siz) sorgusu başarısız")
            no_isrc_tracks = []

        logger.info("Genre enrichment (ISRC'siz, Last.fm) başlıyor: %d track", len(no_isrc_tracks))

        for track in no_isrc_tracks:
            processed += 1
            artists = track.get("artists") or []
            artist = artists[0] if artists else ""
            title  = track.get("title", "")

            if not artist:
                failed += 1
                continue

            time.sleep(_LASTFM_DELAY_S)
            genres = enrich_track_genres_no_isrc(artist, title, http, lastfm_api_key)

            if genres:
                try:
                    supabase_client.table("tracks").update(
                        {"genres": genres}
                    ).eq("id", track["id"]).execute()
                    enriched += 1
                    no_isrc_enriched += 1
                except Exception:
                    logger.warning("tracks.genres yazılamadı (ISRC'siz): id=%s", track["id"])
                    failed += 1
            else:
                failed += 1

    logger.info(
        "Genre enrichment tamamlandı: %d işlendi, %d zenginleşti "
        "(isrc=%d no_isrc=%d), %d başarısız",
        processed, enriched, isrc_enriched, no_isrc_enriched, failed,
    )
    return {
        "processed": processed,
        "enriched": enriched,
        "failed": failed,
        "isrc_enriched": isrc_enriched,
        "no_isrc_enriched": no_isrc_enriched,
    }

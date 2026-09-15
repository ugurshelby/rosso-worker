"""Hiyerarşik tür sözlüğü + ağırlıklı skor + slot seçimi (genre DNA sistemi).

Ham tür/etiket çıktıları farklı kaynaklardan farklı formatlarda gelir:
Deezer kaba tür ('Rap/Hip Hop'), Last.fm ham etiket + count ('hip hop'=100),
MusicBrainz recording/artist tag'leri. Bu modül:

  1. canonical_for      → ham etiketi kanonik türe eşler (alias sözlüğü).
  2. normalize_genres   → (tag, count) + kaynak → ağırlıklı ScoredGenre listesi.
  3. merge_scores       → çok kaynağı birleştirir, frequency bonus uygular.
  4. select_slots       → hiyerarşi filtresi + slot ağırlıkları → genre_data dict.

Temel hiyerarşi kuralı: alt tür seçilmişse üst türü YAZMA (zaten kapsar).
Ama üst tür önce seçildiyse, sonradan gelen alt tür spesifik bilgidir → EKLE.
"""
from __future__ import annotations

from dataclasses import dataclass


# ─────────────────────────────────────────────────────────────────────
# Hiyerarşik tür sözlüğü. parent=None → kök tür; aksi halde üst tür slug'ı.
# ─────────────────────────────────────────────────────────────────────
GENRE_HIERARCHY: dict[str, dict] = {
    # ── HİP-HOP DALI ──────────────────────────────────────────────
    "hip-hop": {"parent": None, "aliases": {"hip hop", "hiphop", "rap", "rap/hip hop",
                "hip-hop", "turkish rap", "türkçe rap", "rap français", "gangsta rap"}},
    "trap": {"parent": "hip-hop", "aliases": {"trap", "trap music", "türkçe trap", "trap rap"}},
    "drill": {"parent": "hip-hop", "aliases": {"drill", "uk drill", "turkish drill"}},
    "lo-fi hip-hop": {"parent": "hip-hop", "aliases": {"lo-fi", "lo fi", "lofi",
                      "lo-fi hip-hop", "chillhop"}},
    "underground hip-hop": {"parent": "hip-hop", "aliases": {"underground hip-hop",
                            "underground hip hop", "underground rap"}},
    "alternative hip-hop": {"parent": "hip-hop", "aliases": {"alternative hip-hop",
                            "alt hip hop", "alternative rap"}},
    "jazz rap": {"parent": "hip-hop", "aliases": {"jazz rap", "jazz hip-hop"}},
    "conscious rap": {"parent": "hip-hop", "aliases": {"conscious", "conscious rap",
                      "conscious hip-hop"}},
    # ── ROK DALI ──────────────────────────────────────────────────
    "rock": {"parent": None, "aliases": {"rock", "rock music", "türkçe rock", "classic rock"}},
    "alternative rock": {"parent": "rock", "aliases": {"alternative rock", "alt rock"}},
    "indie rock": {"parent": "rock", "aliases": {"indie rock"}},
    "garage rock": {"parent": "rock", "aliases": {"garage rock", "garage"}},
    "glam rock": {"parent": "rock", "aliases": {"glam rock", "glam"}},
    "hard rock": {"parent": "rock", "aliases": {"hard rock"}},
    "post-rock": {"parent": "rock", "aliases": {"post-rock", "post rock"}},
    "britpop": {"parent": "rock", "aliases": {"britpop", "brit pop"}},
    "anadolu rock": {"parent": "rock", "aliases": {"anadolu rock", "anatolian rock"}},
    # ── METAL DALI ────────────────────────────────────────────────
    "metal": {"parent": None, "aliases": {"metal", "heavy metal"}},
    "death metal": {"parent": "metal", "aliases": {"death metal"}},
    "black metal": {"parent": "metal", "aliases": {"black metal"}},
    "metalcore": {"parent": "metal", "aliases": {"metalcore", "nu metal"}},
    "industrial metal": {"parent": "metal", "aliases": {"industrial metal"}},
    # ── ELEKTRONİK DALI ───────────────────────────────────────────
    "elektronik": {"parent": None, "aliases": {"electronic", "elektronik", "edm", "electronica",
                   "electro", "elektro"}},
    "house": {"parent": "elektronik", "aliases": {"house", "deep house", "tech house"}},
    "techno": {"parent": "elektronik", "aliases": {"techno", "tekno"}},
    "dubstep": {"parent": "elektronik", "aliases": {"dubstep"}},
    "trip-hop": {"parent": "elektronik", "aliases": {"trip-hop", "trip hop"}},
    "downtempo": {"parent": "elektronik", "aliases": {"downtempo", "chillout", "chillwave"}},
    "industrial": {"parent": "elektronik", "aliases": {"industrial", "industrial rock"}},
    "synthwave": {"parent": "elektronik", "aliases": {"synthwave", "synth wave", "retrowave",
                  "outrun"}},
    "hardstyle": {"parent": "elektronik", "aliases": {"hardstyle", "hardcore", "gabber"}},
    # Deezer üst-tür etiketi (genre_id 113 → "Dans"; EN yüzeyde "Dance").
    # 2026-07-16 ölçümü: bu etiket haritada yoktu → track verisi bulunup boşa
    # düşüyordu (CURSEDEVIL örneği), sanatçı haksız yere pending kalıyordu.
    "dance": {"parent": "elektronik", "aliases": {"dance", "dans", "dance music"}},
    # ambient bağımsız — hem elektronik hem film müziği bağlamında kullanılır
    "ambient": {"parent": None, "aliases": {"ambient", "dark ambient"}},
    # ── POP DALI ──────────────────────────────────────────────────
    "pop": {"parent": None, "aliases": {"pop", "turkish pop", "türkçe pop", "dance pop"}},
    "indie pop": {"parent": "pop", "aliases": {"indie pop"}},
    "art pop": {"parent": "pop", "aliases": {"art pop"}},
    "electropop": {"parent": "pop", "aliases": {"electropop", "synthpop"}},
    "pop rap": {"parent": "pop", "aliases": {"pop rap"}},
    "anadolu pop": {"parent": "pop", "aliases": {"anatolian pop", "anadolu pop"}},
    # ── SOUL/R&B DALI ─────────────────────────────────────────────
    "r&b": {"parent": None, "aliases": {"r&b", "rnb", "rhythm and blues", "contemporary r&b"}},
    "soul": {"parent": "r&b", "aliases": {"soul", "soul & funk"}},
    "neo-soul": {"parent": "r&b", "aliases": {"neo-soul", "neo soul"}},
    "alternative r&b": {"parent": "r&b", "aliases": {"alternative rnb", "alternative r&b",
                        "alt r&b"}},
    # ── JAZZ DALI ─────────────────────────────────────────────────
    "jazz": {"parent": None, "aliases": {"jazz", "caz"}},
    "bebop": {"parent": "jazz", "aliases": {"bebop"}},
    "fusion": {"parent": "jazz", "aliases": {"fusion", "jazz fusion"}},
    "cool jazz": {"parent": "jazz", "aliases": {"cool jazz", "modal jazz"}},
    # ── FOLK DALI ─────────────────────────────────────────────────
    "folk": {"parent": None, "aliases": {"folk", "folk rock", "acoustic", "singer-songwriter"}},
    "indie folk": {"parent": "folk", "aliases": {"indie folk"}},
    "türk halk müziği": {"parent": None, "aliases": {"türk halk müziği", "turkish folk",
                         "thm", "halk müziği"}},
    # ── PUNK DALI ─────────────────────────────────────────────────
    "punk": {"parent": None, "aliases": {"punk", "punk rock"}},
    "pop punk": {"parent": "punk", "aliases": {"pop punk"}},
    # ── FİLM MÜZİĞİ DALI ──────────────────────────────────────────
    "film müzikleri": {"parent": None, "aliases": {"film müzikleri", "film/oyun", "films/games",
                       "soundtrack", "score", "ost", "film score", "game soundtrack"}},
    "instrumental": {"parent": None, "aliases": {"instrumental", "composer"}},
    # ── KLASİK ────────────────────────────────────────────────────
    "klasik": {"parent": None, "aliases": {"classical", "classical music", "orchestral", "klasik"}},
    # ── TÜRK MÜZİĞİ ───────────────────────────────────────────────
    "arabesk": {"parent": None, "aliases": {"arabesk", "damar", "arabesque"}},
    "türk sanat müziği": {"parent": None, "aliases": {"türk sanat müziği", "tsm",
                          "turkish classical music", "sanat müziği", "türk sanat musikisi"}},
    # ── DİĞER ─────────────────────────────────────────────────────
    "reggae": {"parent": None, "aliases": {"reggae", "ska", "dancehall"}},
    "latin": {"parent": None, "aliases": {"latin", "reggaeton", "salsa", "bossa nova",
              "latin müzik", "latin music", "brezilya müziği", "brazilian music", "mpb"}},
    "country": {"parent": None, "aliases": {"country", "americana"}},
    "blues": {"parent": None, "aliases": {"blues"}},
    "funk": {"parent": None, "aliases": {"funk"}},
    "alternatif": {"parent": None, "aliases": {"alternatif", "alternative", "indie"}},
    "experimental": {"parent": None, "aliases": {"experimental"}},
    # Deezer bölgesel üst-türleri (2026-07-16 ölçümü: 8 etiket haritasızdı).
    # Ayrı kanonikler açmak yerine tek "dünya müziği" çatısı — ürün sözlüğünü
    # şişirmeden gerçeği söylüyor. (Kids/Christian/Chanson bilinçli DIŞARIDA:
    # gürültü, tür kimliği taşımıyor.)
    "dünya müziği": {"parent": None, "aliases": {"dünya müziği", "world music", "world",
                     "afrika müziği", "african music", "arap müziği", "arabic music",
                     "asya müziği", "asian music", "hint müziği", "indian music"}},
}

# Alias → kanonik ters harita (hızlı arama).
_ALIAS_TO_CANONICAL: dict[str, str] = {
    alias: canonical
    for canonical, meta in GENRE_HIERARCHY.items()
    for alias in meta["aliases"]
}
# Kanonik adının kendisi de alias sayılır ('trap' → 'trap').
for _canon in GENRE_HIERARCHY:
    _ALIAS_TO_CANONICAL.setdefault(_canon, _canon)


# ─────────────────────────────────────────────────────────────────────
# Skor sabitleri
# ─────────────────────────────────────────────────────────────────────
# Kaynak güvenilirlik ağırlıkları (spesifik → genel).
SOURCE_WEIGHTS: dict[str, float] = {
    "lastfm_track":       1.0,   # en spesifik: bu şarkının tag'leri
    "deezer_track":       0.9,
    "musicbrainz_rec":    0.85,  # recording araması
    "db_tracks":          0.7,   # kendi verimiz (aynı şarkının başka versiyonu)
    "lastfm_artist":      0.6,
    "musicbrainz_artist": 0.5,
    "deezer_artist":      0.3,   # en az güvenilir (yanlış eşleşme riski)
}

# count vermeyen kaynaklarda pozisyona göre azalan varsayılan "count"
# (soldaki tag daha baskın). 6. ve sonrası için son değer kullanılır.
_POSITION_COUNTS = [50, 35, 25, 15, 10]

# Kaç farklı kaynakta geçti → çarpan bonusu.
FREQUENCY_BONUS: dict[int, float] = {1: 1.0, 2: 1.2, 3: 1.5}  # 4+ → 1.7

# Slot ağırlıkları (track 3 slot, artist 5 slot).
_SLOT_WEIGHTS: dict[int, list[float]] = {
    3: [0.60, 0.30, 0.10],
    5: [0.40, 0.25, 0.15, 0.12, 0.08],
}


@dataclass
class ScoredGenre:
    """Bir kaynaktan gelen tek kanonik türün ham skoru."""
    canonical: str
    score: float
    source: str


def canonical_for(tag: str | None) -> str | None:
    """Ham etiketi kanonik türe eşle (büyük/küçük harf duyarsız). Yoksa None."""
    if not tag or not isinstance(tag, str):
        return None
    return _ALIAS_TO_CANONICAL.get(tag.strip().lower())


# ─────────────────────────────────────────────────────────────────────
# Aile akrabalığı — track-tabanlı süzme (artist-level çakışma koruması)
# ─────────────────────────────────────────────────────────────────────
# Elle tanımlı yakın-tür komşulukları (parent ağacında görünmeyen gerçek
# müzikal akrabalıklar). Simetrik kontrol edilir; tek yön yazmak yeter.
GENRE_KINSHIP: dict[str, set[str]] = {
    "pop":       {"r&b", "elektronik"},
    # electropop/synthpop pop dalında ama elektronik üretimli — elektronik ailesiyle
    # akraba (Otnicka/Essenger temiz sanatçıları %0 yanlış-tutarlılık alıyordu).
    "electropop": {"elektronik"},
    "r&b":     {"pop", "hip-hop", "soul"},
    "hip-hop": {"r&b", "pop", "pop rap"},
    # pop rap taksonomide pop kökünde (parent=pop) ama gerçekte hip-hop alt-akımı.
    # Kid Ink/Dr. Dre/2Pac gibi gerçek rapçilerde hip-hop+pop rap birlikte gelir
    # ve yanlışlıkla tutarsız (SUSPECT) sayılmamalı (2026-07-05, gerçek DB).
    "pop rap": {"hip-hop"},
    "rock":    {"metal", "punk", "alternatif"},
    "metal":   {"rock"},
    "punk":    {"rock"},
    "folk":    {"country", "blues"},
    "blues":   {"jazz", "soul", "folk"},
    "jazz":    {"blues", "soul"},
    "soul":    {"r&b", "jazz", "blues"},
}

# Cross-genre türler: herhangi bir türle akraba sayılır (bağlamdan bağımsız).
CROSS_GENRE: set[str] = {"ambient", "instrumental", "film müzikleri", "experimental"}


def _root_of(genre: str) -> str:
    """Türün kök atasını bul (parent zincirini yukarı takip et)."""
    current = genre
    seen: set[str] = set()
    while current not in seen:
        seen.add(current)
        parent = GENRE_HIERARCHY.get(current, {}).get("parent")
        if parent is None:
            return current
        current = parent
    return current


def are_related(a: str | None, b: str | None) -> bool:
    """İki kanonik tür 'aynı aile' mi? (track-tabanlı aile-süzme için).

    Kabul (biri yeterli):
      1. Aynı tür.
      2. Cross-genre muafiyet (a veya b bağlamdan bağımsız).
      3. Parent zinciri / ortak kök (drill & trap → hip-hop).
      4. GENRE_KINSHIP elle tanımlı komşuluk (simetrik).
    Bilinmeyen (sözlükte olmayan) tür → False.
    """
    if not a or not b:
        return False
    if a == b:
        return True
    # Cross-genre her şeyle akraba
    if a in CROSS_GENRE or b in CROSS_GENRE:
        return True
    # Her ikisi de sözlükte olmalı (bilinmeyen → akraba değil)
    if a not in GENRE_HIERARCHY or b not in GENRE_HIERARCHY:
        return False
    # Parent zinciri / ortak kök
    if _root_of(a) == _root_of(b):
        return True
    # Elle tanımlı komşuluk (simetrik)
    if b in GENRE_KINSHIP.get(a, set()) or a in GENRE_KINSHIP.get(b, set()):
        return True
    return False


def _position_count(index: int) -> int:
    """count vermeyen kaynakta index'e göre azalan varsayılan count."""
    if index < len(_POSITION_COUNTS):
        return _POSITION_COUNTS[index]
    return _POSITION_COUNTS[-1]


def normalize_genres(
    raw: list[tuple[str, int]] | None,
    source: str,
) -> list[ScoredGenre]:
    """(tag, count) çiftlerini kanonik türe eşle, kaynak+count ile ham skor üret.

    - count > 0 ise Last.fm gerçek popülerliği (0-100) kabul edilir.
    - count == 0 ise kaynak count vermemiştir → pozisyona göre azalan varsayılan
      count atanır (ilk tag baskın).
    - Aynı kanonik türe eşlenen birden çok ham tag skorları birikir.
    - Gürültü/sözlükte olmayan etiket düşürülür.

    Döner: kanonik başına tek ScoredGenre (skorlar toplanmış).
    """
    if not raw:
        return []

    source_weight = SOURCE_WEIGHTS.get(source, 0.5)
    accumulated: dict[str, float] = {}

    for index, item in enumerate(raw):
        tag, count = item
        canonical = canonical_for(tag)
        if canonical is None:
            continue
        effective_count = count if count and count > 0 else _position_count(index)
        score = source_weight * (effective_count / 100.0)
        accumulated[canonical] = accumulated.get(canonical, 0.0) + score

    return [ScoredGenre(c, s, source) for c, s in accumulated.items()]


def merge_scores(
    scored_lists: list[list[ScoredGenre]],
) -> dict[str, dict]:
    """Birden çok kaynağın ScoredGenre listelerini birleştir, frequency bonus uygula.

    Bir kanonik tür kaç farklı kaynakta geçtiyse skoru o oranda yükseltilir
    (FREQUENCY_BONUS). Döner: {canonical: {"score": float, "sources": [str, ...]}}.
    """
    base: dict[str, float] = {}
    sources: dict[str, list[str]] = {}

    for scored in scored_lists:
        for sg in scored:
            base[sg.canonical] = base.get(sg.canonical, 0.0) + sg.score
            sources.setdefault(sg.canonical, [])
            if sg.source not in sources[sg.canonical]:
                sources[sg.canonical].append(sg.source)

    merged: dict[str, dict] = {}
    for canonical, raw_score in base.items():
        freq = len(sources[canonical])
        bonus = FREQUENCY_BONUS.get(freq, 1.7)  # 4+ kaynak → 1.7
        merged[canonical] = {
            "score": round(raw_score * bonus, 4),
            "sources": sources[canonical],
        }
    return merged


def _passes_hierarchy(candidate: str, chosen: list[str]) -> bool:
    """Hiyerarşi filtresi: candidate seçili listeye eklenmeli mi?

    - candidate'ın bir ALT türü zaten seçilmişse → candidate gereksiz üst tür, ATLA.
    - Aksi halde (parent zaten seçili olsa bile spesifik bilgi) → EKLE.
    """
    for c in chosen:
        parent = GENRE_HIERARCHY.get(c, {}).get("parent")
        if parent == candidate:
            # candidate, seçili bir türün üst türü → gereksiz, atla
            return False
    return True


def select_slots(merged: dict[str, dict], max_slots: int) -> dict:
    """Birleştirilmiş skorlardan hiyerarşi filtresi + slot ağırlıkları ile genre_data üret.

    Döner:
      {
        "slots":      [kanonik, ...],          # en fazla max_slots
        "weights":    [float, ...],            # slot ağırlıkları
        "raw_scores": {kanonik: float, ...},   # tüm aday ham skorlar
        "sources":    {kanonik: [str, ...]},
      }
    """
    raw_scores = {c: info["score"] for c, info in merged.items()}
    sources = {c: info["sources"] for c, info in merged.items()}

    # Skora göre azalan sırala (eşitlikte kanonik ada göre kararlı sıra).
    ordered = sorted(merged.items(), key=lambda kv: (-kv[1]["score"], kv[0]))

    chosen: list[str] = []
    for canonical, _info in ordered:
        if len(chosen) >= max_slots:
            break
        if _passes_hierarchy(canonical, chosen):
            chosen.append(canonical)

    weights_template = _SLOT_WEIGHTS.get(max_slots, [])
    weights = weights_template[: len(chosen)]

    return {
        "slots": chosen,
        "weights": weights,
        "raw_scores": raw_scores,
        "sources": sources,
    }

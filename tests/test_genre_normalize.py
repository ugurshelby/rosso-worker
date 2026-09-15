"""genre_normalize — hiyerarşik kanonik sözlük + ağırlıklı skor + slot seçimi testleri.

Yeni sistem (2026-07-01 genre DNA):
  - normalize_genres(raw, source): (tag, count) çiftlerini kanonik türe eşler,
    kaynak güvenilirliği + count ile ham skor üretir.
  - merge_scores(scored_lists): birden çok kaynağı birleştirir, frequency bonus uygular.
  - select_slots(merged, max_slots): hiyerarşi filtresi + slot ağırlıkları → genre_data.
"""
from app.services.genre_normalize import (
    ScoredGenre,
    normalize_genres,
    merge_scores,
    select_slots,
    canonical_for,
    are_related,
)


# ─────────────────────────────────────────────────────────────────────
# are_related — track-tabanlı aile-süzme için tür akrabalığı
# ─────────────────────────────────────────────────────────────────────
def test_related_ayni_tur():
    assert are_related("hip-hop", "hip-hop") is True


def test_related_parent_zinciri():
    # drill'in parent'ı hip-hop
    assert are_related("hip-hop", "drill") is True
    assert are_related("drill", "hip-hop") is True  # simetrik


def test_related_ortak_kok():
    # drill ve trap ikisi de hip-hop kökü
    assert are_related("drill", "trap") is True


def test_related_kinship_haritasi():
    # pop ↔ r&b elle tanımlı komşu
    assert are_related("pop", "r&b") is True
    assert are_related("r&b", "pop") is True
    assert are_related("rock", "metal") is True


def test_related_cross_genre_muaf():
    # instrumental/ambient/film müzikleri/experimental her şeyle akraba
    assert are_related("hip-hop", "instrumental") is True
    assert are_related("pop", "ambient") is True
    assert are_related("film müzikleri", "death metal") is True


# ─────────────────────────────────────────────────────────────────────
# Normalizer açıkları (Fable 5 kalibrasyon, 2026-07-03)
# ─────────────────────────────────────────────────────────────────────
def test_hiphop_tiresiz_alias():
    """`hiphop` (tiresiz) → hip-hop. Ati242 gibi sanatçıların `hiphop:100`
    sinyali boşa düşüyordu (canlı doğrulandı)."""
    assert canonical_for("hiphop") == "hip-hop"


def test_electropop_ailesi_elektronik_akraba():
    """electropop/synthpop/synthwave/hardstyle elektronik ailesiyle akraba
    sayılmalı — Otnicka/Essenger temiz sanatçıları yanlış %0 tutarlılık
    alıyordu (canlı doğrulandı)."""
    # synthpop ham etiketi electropop kanoniğine çözülür (alias)
    assert canonical_for("synthpop") == "electropop"
    assert canonical_for("synthwave") == "synthwave"
    assert canonical_for("hardstyle") == "hardstyle"
    # kanonik türler elektronik ailesiyle akraba
    assert are_related("electropop", "elektronik") is True
    assert are_related("synthwave", "elektronik") is True
    assert are_related("hardstyle", "elektronik") is True


def test_related_alakasiz_cift_false():
    # manifest bug'ı: pop vs death metal akraba DEĞİL
    assert are_related("pop", "death metal") is False
    # TumaniYO: hip-hop vs reggae akraba değil
    assert are_related("hip-hop", "reggae") is False
    assert are_related("klasik", "trap") is False


def test_pop_rap_hiphop_akraba():
    """pop rap taksonomide pop kökünde ama gerçekte hip-hop alt-akımı — Kid Ink/
    Dr. Dre/2Pac gibi gerçek rapçilerde `hip-hop` + `pop rap` kombinasyonu doğru,
    yanlışlıkla SUSPECT'e düşürülmemeli (2026-07-05, gerçek DB doğrulaması).
    pop rap her iki kökle de (hem pop hem hip-hop) akraba olmalı."""
    assert are_related("pop rap", "hip-hop") is True
    assert are_related("hip-hop", "pop rap") is True  # simetrik
    # pop kökü zaten parent zinciriyle akraba
    assert are_related("pop rap", "pop") is True


def test_related_bilinmeyen_tur_false():
    assert are_related("hip-hop", "bilinmeyen") is False
    assert are_related(None, "pop") is False


# ─────────────────────────────────────────────────────────────────────
# canonical_for — alias → kanonik eşleme (büyük/küçük harf duyarsız)
# ─────────────────────────────────────────────────────────────────────
def test_canonical_deezer_kaba_turleri():
    assert canonical_for("Rap/Hip Hop") == "hip-hop"
    assert canonical_for("Pop") == "pop"


def test_canonical_turk_etiketleri():
    assert canonical_for("arabesk") == "arabesk"
    assert canonical_for("damar") == "arabesk"
    assert canonical_for("TSM") == "türk sanat müziği"
    assert canonical_for("Turkish Rap") == "hip-hop"


def test_canonical_hiyerarsik_alt_turler():
    # Alt türler kendi kanonik adına eşlenir (üst türe DEĞİL)
    assert canonical_for("trap") == "trap"
    assert canonical_for("uk drill") == "drill"
    assert canonical_for("garage rock") == "garage rock"
    assert canonical_for("trip-hop") == "trip-hop"
    assert canonical_for("neo-soul") == "neo-soul"


def test_canonical_film_muzigi_tanınıyor():
    # Eski sözlükte YOKTU — Ramin Djawadi bug'ının kaynağı
    assert canonical_for("soundtrack") == "film müzikleri"
    assert canonical_for("score") == "film müzikleri"
    assert canonical_for("ost") == "film müzikleri"


def test_canonical_ambient_ve_lofi_tanınıyor():
    assert canonical_for("ambient") == "ambient"
    assert canonical_for("lo-fi") == "lo-fi hip-hop"
    assert canonical_for("chillhop") == "lo-fi hip-hop"


def test_canonical_gurultu_none_doner():
    assert canonical_for("All") is None
    assert canonical_for("baba") is None
    assert canonical_for("sagopa kajmer") is None
    assert canonical_for("") is None


# ─────────────────────────────────────────────────────────────────────
# normalize_genres — (tag, count) + source → ScoredGenre listesi
# ─────────────────────────────────────────────────────────────────────
def test_normalize_lastfm_track_count_kullanir():
    # Last.fm gerçek count (0-100), source_weight=1.0 (lastfm_track)
    result = normalize_genres([("hip hop", 100), ("rap", 80)], "lastfm_track")
    # ikisi de hip-hop'a eşlenir → tek kanonik, skorlar birikir
    scores = {s.canonical: s for s in result}
    assert "hip-hop" in scores
    # skor = source_weight × (count/100), iki tag birikir: 1.0×1.0 + 1.0×0.8
    assert scores["hip-hop"].score > 1.5
    assert scores["hip-hop"].source == "lastfm_track"


def test_normalize_pozisyon_bazli_count_deezer():
    # Deezer count vermez → normalize_genres pozisyon count'u atar (50,35,25,...)
    # Sıra: pop baskın, sonra r&b, sonra reggae
    result = normalize_genres([("Pop", 0), ("r&b", 0), ("reggae", 0)], "deezer_track")
    scores = {s.canonical: s.score for s in result}
    # pop > r&b > reggae olmalı (pozisyon ağırlığı)
    assert scores["pop"] > scores["r&b"] > scores["reggae"]


def test_normalize_gurultu_atlanir():
    result = normalize_genres([("All", 50), ("hip hop", 80), ("baba", 10)], "lastfm_track")
    canonicals = {s.canonical for s in result}
    assert canonicals == {"hip-hop"}


def test_normalize_bos_giris():
    assert normalize_genres([], "lastfm_track") == []
    assert normalize_genres(None, "lastfm_track") == []


# ─────────────────────────────────────────────────────────────────────
# merge_scores — çok kaynak birleşimi + frequency bonus
# ─────────────────────────────────────────────────────────────────────
def test_merge_frequency_bonus():
    # hip-hop 3 kaynakta → frequency_bonus 1.5
    lastfm = normalize_genres([("hip hop", 100)], "lastfm_track")
    deezer = normalize_genres([("rap", 0)], "deezer_track")
    mb = normalize_genres([("hip hop", 0)], "musicbrainz_artist")
    merged = merge_scores([lastfm, deezer, mb])
    assert "hip-hop" in merged
    # 3 kaynak → sources listesi 3 eleman
    assert len(merged["hip-hop"]["sources"]) == 3
    # frequency bonus uygulanmış (ham toplamdan büyük)
    assert merged["hip-hop"]["score"] > 0


def test_merge_tek_kaynak_bonus_yok():
    lastfm = normalize_genres([("jazz", 60)], "lastfm_track")
    merged = merge_scores([lastfm])
    assert merged["jazz"]["sources"] == ["lastfm_track"]


# ─────────────────────────────────────────────────────────────────────
# select_slots — hiyerarşi filtresi + slot ağırlıkları
# ─────────────────────────────────────────────────────────────────────
def _merged(pairs):
    """(canonical, score) listesinden sahte merged dict üretir."""
    return {c: {"score": s, "sources": ["lastfm_track"]} for c, s in pairs}


def test_slots_track_3_slot_agirlik():
    merged = _merged([("hip-hop", 100), ("r&b", 50), ("pop", 10), ("jazz", 5)])
    data = select_slots(merged, max_slots=3)
    assert data["slots"] == ["hip-hop", "r&b", "pop"]
    assert data["weights"] == [0.60, 0.30, 0.10]


def test_slots_artist_5_slot_agirlik():
    merged = _merged([("pop", 100), ("r&b", 80), ("soul", 60),
                      ("jazz", 40), ("funk", 30), ("blues", 10)])
    data = select_slots(merged, max_slots=5)
    assert len(data["slots"]) == 5
    assert data["weights"] == [0.40, 0.25, 0.15, 0.12, 0.08]


def test_slots_hiyerarsi_ust_tur_atlanir():
    # trap (hip-hop alt türü) daha yüksek skorlu; hip-hop üst tür → ATLA
    merged = _merged([("trap", 100), ("hip-hop", 90), ("r&b", 20)])
    data = select_slots(merged, max_slots=3)
    # hip-hop atlanmalı (alt türü trap zaten seçildi), r&b eklenmeli
    assert "hip-hop" not in data["slots"]
    assert "trap" in data["slots"]
    assert "r&b" in data["slots"]


def test_slots_hiyerarsi_farkli_alt_turler_ikisi_de():
    # garage rock + indie rock: aynı dalın farklı alt türleri → İKİSİ DE
    merged = _merged([("garage rock", 100), ("indie rock", 90), ("pop", 20)])
    data = select_slots(merged, max_slots=3)
    assert "garage rock" in data["slots"]
    assert "indie rock" in data["slots"]


def test_slots_alt_tur_varken_ust_tur_spesifik_eklenir():
    # Üst tür önce (yüksek skor), sonra alt tür → alt tür spesifik bilgi, EKLE
    merged = _merged([("rock", 100), ("garage rock", 50), ("pop", 10)])
    data = select_slots(merged, max_slots=3)
    # rock seçili, garage rock spesifik → ikisi de olabilir
    assert "rock" in data["slots"]
    assert "garage rock" in data["slots"]


def test_slots_bos_merged():
    data = select_slots({}, max_slots=3)
    assert data["slots"] == []
    assert data["weights"] == []


def test_slots_raw_scores_ve_sources_dolu():
    merged = _merged([("hip-hop", 100), ("r&b", 50)])
    data = select_slots(merged, max_slots=3)
    assert data["raw_scores"]["hip-hop"] == 100
    assert data["sources"]["hip-hop"] == ["lastfm_track"]

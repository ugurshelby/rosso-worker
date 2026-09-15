"""deezer_genre — artist+title → album genre testleri (fake http)."""
from app.services.deezer_genre import get_deezer_genres


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
    def raise_for_status(self):
        pass
    def json(self):
        return self._payload


class _FakeHttp:
    """search → album_id; album → genres döndüren sahte http client."""
    def __init__(self, search_payload, album_payload):
        self._search = search_payload
        self._album = album_payload
        self.calls = []
    def get(self, url, **kwargs):
        self.calls.append(url)
        if "/search" in url:
            return _FakeResp(self._search)
        if "/album/" in url:
            return _FakeResp(self._album)
        return _FakeResp({})


def test_artist_title_album_genre():
    http = _FakeHttp(
        search_payload={"data": [{"id": 1, "album": {"id": 99}, "artist": {"id": 5}}]},
        album_payload={"genres": {"data": [{"name": "Rap/Hip Hop"}]}},
    )
    genres = get_deezer_genres("Ezhel", "Geceler", http)
    assert genres == ["Rap/Hip Hop"]
    # search + album çağrıldı
    assert any("/search" in c for c in http.calls)
    assert any("/album/99" in c for c in http.calls)


def test_track_bulunamadi():
    http = _FakeHttp(search_payload={"data": []}, album_payload={})
    assert get_deezer_genres("Yok", "Yok", http) == []


def test_album_genre_bos():
    http = _FakeHttp(
        search_payload={"data": [{"id": 1, "album": {"id": 99}}]},
        album_payload={"genres": {"data": []}},
    )
    assert get_deezer_genres("Müslüm Gürses", "Affet", http) == []


def test_get_deezer_artist_genres_bulur(monkeypatch):
    """Sanatçı araması başarılıysa genre listesi döner."""
    search_resp = {
        "data": [{
            "artist": {"id": 183499},
            "album": {"id": 999}
        }]
    }
    album_resp = {"genres": {"data": [{"name": "Post-Rock"}, {"name": "Ambient"}]}}

    class _HTTP:
        def get(self, url, timeout=10):
            class _R:
                def raise_for_status(self): pass
                def json(self_):
                    if "/artist/" in url and "top" in url:
                        # /artist/{id}/top endpoint'i — track listesi döner (album.id için)
                        return {"data": [{"album": {"id": 999}}]}
                    if "/album/" in url:
                        return album_resp
                    return search_resp
            return _R()

    from app.services.deezer_genre import get_deezer_artist_genres
    result = get_deezer_artist_genres("April Rain", _HTTP())
    assert isinstance(result, list)
    assert len(result) >= 1
    assert "Post-Rock" in result


def test_get_deezer_artist_genres_bulamazsa_bos(monkeypatch):
    """Sanatçı bulunamazsa boş liste döner, exception fırlatmaz."""
    class _HTTP:
        def get(self, url, timeout=10):
            class _R:
                def raise_for_status(self): pass
                def json(self): return {"data": []}
            return _R()

    from app.services.deezer_genre import get_deezer_artist_genres
    result = get_deezer_artist_genres("BilinmeyenSanatci", _HTTP())
    assert result == []


def test_get_deezer_artist_genres_http_hatasi():
    """HTTP hatası exception fırlatmaz, boş döner."""
    class _HTTP:
        def get(self, url, timeout=10):
            raise Exception("connection error")

    from app.services.deezer_genre import get_deezer_artist_genres
    result = get_deezer_artist_genres("April Rain", _HTTP())
    assert result == []


def test_get_deezer_artist_genres_bos_artist():
    """Boş sanatçı adı için boş liste döner."""
    class _HTTP:
        def get(self, url, timeout=10):
            raise AssertionError("HTTP çağrısı yapılmamalı")

    from app.services.deezer_genre import get_deezer_artist_genres
    result = get_deezer_artist_genres("", _HTTP())
    assert result == []


def test_track_scored_with_anchor_returns_artist_name():
    """Track hit'inden hem tür hem doğrulanmış sanatçı adı (çapa) döner."""
    from app.services.deezer_genre import get_deezer_track_scored_with_anchor

    class _Resp:
        def __init__(self, payload): self._p = payload
        def raise_for_status(self): pass
        def json(self): return self._p

    class _Http:
        def __init__(self): self.calls = 0
        def get(self, url, timeout=10):
            self.calls += 1
            if "search" in url:
                return _Resp({"data": [{
                    "artist": {"id": 301153511, "name": "manifest"},
                    "album": {"id": 754440531},
                }]})
            return _Resp({"genres": {"data": [{"name": "Pop"}]}})

    scored, anchor, _ = get_deezer_track_scored_with_anchor("manifest", "Yaşanacaksa", _Http())
    assert anchor == "manifest"
    assert ("Pop", 0) in scored


def test_track_scored_with_anchor_no_result():
    """Track bulunamazsa ([], None)."""
    from app.services.deezer_genre import get_deezer_track_scored_with_anchor

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"data": []}

    class _Http:
        def get(self, url, timeout=10): return _Resp()

    scored, anchor, _ = get_deezer_track_scored_with_anchor("X", "Y", _Http())
    assert scored == []
    assert anchor is None


def test_track_anchor_429_raises_ratelimit():
    """Deezer search 429 → RateLimitError(provider='deezer')."""
    import httpx
    import pytest
    from app.services.deezer_genre import get_deezer_track_scored_with_anchor
    from app.services.genre_errors import RateLimitError

    class _Resp:
        status_code = 429
        headers = {"Retry-After": "30"}
        def raise_for_status(self):
            raise httpx.HTTPStatusError("429", request=None, response=self)
        def json(self): return {}

    class _Http:
        def get(self, url, timeout=10): return _Resp()

    with pytest.raises(RateLimitError) as exc:
        get_deezer_track_scored_with_anchor("X", "Y", _Http())
    assert exc.value.provider == "deezer"
    assert exc.value.retry_after == 30.0


def test_numeric_title_skips_deezer_query_entirely():
    """Tamamen sayısal başlık (örn. '129') Deezer'a hiç sorgu atmaz, boş döner."""
    from app.services.deezer_genre import get_deezer_track_scored_with_anchor

    calls = []

    class _Http:
        def get(self, url, timeout=10):
            calls.append(url)
            raise AssertionError("Deezer'a sorgu atılmamalıydı")

    result, anchor, _ = get_deezer_track_scored_with_anchor("Şehinşah", "129", _Http())

    assert result == []
    assert anchor is None
    assert calls == []


def test_numeric_title_with_whitespace_also_skipped():
    """Baştaki/sondaki boşluklu sayısal başlık da (' 129 ') atlanır."""
    from app.services.deezer_genre import get_deezer_track_scored_with_anchor

    class _Http:
        def get(self, url, timeout=10):
            raise AssertionError("Deezer'a sorgu atılmamalıydı")

    result, anchor, _ = get_deezer_track_scored_with_anchor("Şehinşah", " 129 ", _Http())
    assert result == []
    assert anchor is None


def test_non_numeric_title_still_queries_deezer():
    """Normal başlık (sayısal olmayan) sorgu atar. İki aşamalı: fold-exact boş
    dönerse düz arama da denenir → 2 search çağrısı (Fable 5, 2026-07-03)."""
    from app.services.deezer_genre import get_deezer_track_scored_with_anchor

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"data": []}

    class _Http:
        def __init__(self): self.calls = 0
        def get(self, url, timeout=10):
            self.calls += 1
            return _Resp()

    http = _Http()
    get_deezer_track_scored_with_anchor("Şehinşah", "Pirana", http)
    # Aşama A (fold-exact) + Aşama B (düz) = 2 search çağrısı
    assert http.calls == 2


# ─────────────────────────────────────────────────────────────────────
# fold() — unicode-fold arama düzeltmesi (Fable 5, 2026-07-03)
# İ'li track'lerde Deezer 5/5 kurtarma. İ→I ön-değişim + NFD + combining at + lower.
# ─────────────────────────────────────────────────────────────────────
def test_fold_turkce_i_ve_aksanlar():
    from app.services.deezer_genre import fold
    assert fold("DAİM") == "daim"
    assert fold("Gülşen") == "gulsen"
    assert fold("İstanbul") == "istanbul"
    assert fold("Ceza") == "ceza"          # aksan yok → sadece lower
    assert fold("") == ""


def test_iki_asamali_stage_a_fold_exact():
    """Aşama A: fold'lu artist:"..." track:"..." kesin araması. İ'li giriş
    fold'lanmış sorguyla eşleşir → tür döner + doğru çapa."""
    from app.services.deezer_genre import get_deezer_track_scored_with_anchor

    class _Resp:
        def __init__(self, p): self._p = p
        def raise_for_status(self): pass
        def json(self): return self._p

    class _Http:
        def __init__(self): self.urls = []
        def get(self, url, timeout=10):
            self.urls.append(url)
            if "search" in url:
                # fold'lu sorgu geldiyse hit döndür
                if "daim" in url.lower():
                    return _Resp({"data": [{
                        "artist": {"id": 1, "name": "Gülşen"},
                        "album": {"id": 50},
                    }]})
                return _Resp({"data": []})
            return _Resp({"genres": {"data": [{"name": "Pop"}]}})

    http = _Http()
    scored, anchor, _ = get_deezer_track_scored_with_anchor("Gülşen", "DAİM", http)
    assert ("Pop", 0) in scored
    assert anchor == "Gülşen"


def test_stage_b_plain_search_with_artist_verification():
    """Aşama B: fold-exact boş dönerse düz 'A T' araması + sanatçı doğrulaması.
    Doğru sanatçı bulunursa kabul."""
    from app.services.deezer_genre import get_deezer_track_scored_with_anchor

    class _Resp:
        def __init__(self, p): self._p = p
        def raise_for_status(self): pass
        def json(self): return self._p

    class _Http:
        def __init__(self): self.search_count = 0
        def get(self, url, timeout=10):
            if "search" in url:
                self.search_count += 1
                # 1. çağrı (fold-exact) boş; 2. çağrı (düz) hit
                if self.search_count == 1:
                    return _Resp({"data": []})
                return _Resp({"data": [{
                    "artist": {"id": 2, "name": "Ceza"},
                    "album": {"id": 60},
                    "title": "Holocaust",
                }]})
            return _Resp({"genres": {"data": [{"name": "Rap/Hip Hop"}]}})

    http = _Http()
    scored, anchor, _ = get_deezer_track_scored_with_anchor("Ceza", "Holocaust", http)
    assert ("Rap/Hip Hop", 0) in scored
    assert anchor == "Ceza"
    assert http.search_count == 2  # iki aşama da denendi


def test_stage_b_rejects_wrong_artist():
    """Aşama B: düz arama yanlış sanatçının track'ini döndürürse (artist <0.85)
    REDDEDİLİR — 'Stabil - Kovala' sorgusu Ati242 döndürdü senaryosu."""
    from app.services.deezer_genre import get_deezer_track_scored_with_anchor

    class _Resp:
        def __init__(self, p): self._p = p
        def raise_for_status(self): pass
        def json(self): return self._p

    class _Http:
        def __init__(self): self.search_count = 0
        def get(self, url, timeout=10):
            if "search" in url:
                self.search_count += 1
                if self.search_count == 1:
                    return _Resp({"data": []})  # fold-exact boş
                # düz arama YANLIŞ sanatçı döndürüyor
                return _Resp({"data": [{
                    "artist": {"id": 9, "name": "Ati242"},
                    "album": {"id": 70},
                    "title": "Kovala",
                }]})
            return _Resp({"genres": {"data": [{"name": "Trap"}]}})

    http = _Http()
    scored, anchor, _ = get_deezer_track_scored_with_anchor("Stabil", "Kovala", http)
    # yanlış sanatçı → tür kabul edilmez
    assert scored == []


# ─────────────────────────────────────────────────────────────────────
# Deezer artist DNA — çoklu-albüm genre_id çoğunluğu (2026-07-04, Motive fix)
# Kök neden: /artist/{id}/top → tek albüm → genres.data (TR'de sık boş, asıl tür
# genre_id'de). Motive → yanlış "elektronik"/"pop". Fix: tüm albümlerin genre_id
# çoğunluğu (Motive 42/48 = Rap/Hip Hop).
# ─────────────────────────────────────────────────────────────────────
def test_deezer_genre_map_ids():
    """Deezer sabit tür-ID haritası doğru isimleri döndürür."""
    from app.services.deezer_genre import _DEEZER_GENRE_MAP

    assert _DEEZER_GENRE_MAP[116] == "Rap/Hip Hop"
    assert _DEEZER_GENRE_MAP[132] == "Pop"
    assert _DEEZER_GENRE_MAP[152] == "Rock"
    assert _DEEZER_GENRE_MAP[464] == "Metal"
    assert _DEEZER_GENRE_MAP[106] == "Elektronik"


def test_artist_dna_album_genre_id_majority():
    """Motive senaryosu: 48 albümün 42'si genre_id 116 (Rap/Hip Hop) → çoğunluk
    Rap/Hip Hop döner. Tek top-track albümüne DEĞİL, tüm albüm dağılımına bakar."""
    from app.services.deezer_genre import get_deezer_artist_genres_scored

    albums = (
        [{"genre_id": 116} for _ in range(42)]
        + [{"genre_id": 106} for _ in range(2)]
        + [{"genre_id": 132} for _ in range(2)]
        + [{"genre_id": -1}, {"genre_id": 85}]
    )

    class _Resp:
        def __init__(self, p): self._p = p
        def raise_for_status(self): pass
        def json(self): return self._p

    class _Http:
        def get(self, url, timeout=10):
            if "/search" in url:
                return _Resp({"data": [{"artist": {"id": 72196, "name": "Motive"}}]})
            if "/albums" in url:
                return _Resp({"data": albums})
            return _Resp({})

    scored, found = get_deezer_artist_genres_scored("Motive", _Http())
    names = [name for name, _ in scored]
    assert found == "Motive"
    assert names[0] == "Rap/Hip Hop"          # çoğunluk ilk sırada
    top_name, top_count = scored[0]
    assert top_count == 42                      # gerçek frekans (count)


def test_artist_dna_uses_albums_endpoint_not_top():
    """/artist/{id}/albums çağrılır, /top DEĞİL (tek albüm tuzağından kaçınmak için)."""
    from app.services.deezer_genre import get_deezer_artist_genres_scored

    class _Resp:
        def __init__(self, p): self._p = p
        def raise_for_status(self): pass
        def json(self): return self._p

    class _Http:
        def __init__(self): self.urls = []
        def get(self, url, timeout=10):
            self.urls.append(url)
            if "/search" in url:
                return _Resp({"data": [{"artist": {"id": 1, "name": "X"}}]})
            if "/albums" in url:
                return _Resp({"data": [{"genre_id": 152}]})
            return _Resp({})

    http = _Http()
    get_deezer_artist_genres_scored("X", http)
    assert any("/albums" in u for u in http.urls)
    assert not any("/top" in u for u in http.urls)


def test_artist_dna_no_albums_returns_empty():
    """Albüm yoksa (veya hepsi geçersiz genre_id) tür boş, sanatçı adı yine döner."""
    from app.services.deezer_genre import get_deezer_artist_genres_scored

    class _Resp:
        def __init__(self, p): self._p = p
        def raise_for_status(self): pass
        def json(self): return self._p

    class _Http:
        def get(self, url, timeout=10):
            if "/search" in url:
                return _Resp({"data": [{"artist": {"id": 5, "name": "Yeni"}}]})
            if "/albums" in url:
                return _Resp({"data": []})
            return _Resp({})

    scored, found = get_deezer_artist_genres_scored("Yeni", _Http())
    assert scored == []
    assert found == "Yeni"


def test_artist_dna_unknown_genre_ids_ignored():
    """Haritada olmayan genre_id (-1, 0) sayıma girmez; kalan çoğunluk döner."""
    from app.services.deezer_genre import get_deezer_artist_genres_scored

    albums = [{"genre_id": -1}, {"genre_id": 0}, {"genre_id": 464}, {"genre_id": 464}]

    class _Resp:
        def __init__(self, p): self._p = p
        def raise_for_status(self): pass
        def json(self): return self._p

    class _Http:
        def get(self, url, timeout=10):
            if "/search" in url:
                return _Resp({"data": [{"artist": {"id": 7, "name": "M"}}]})
            if "/albums" in url:
                return _Resp({"data": albums})
            return _Resp({})

    scored, _ = get_deezer_artist_genres_scored("M", _Http())
    names = [n for n, _ in scored]
    assert names == ["Metal"]  # sadece geçerli id'ler (464×2)


# ─────────────────────────────────────────────────────────────────────
# artist.id çapası — track hit'inden doğru artist'e (2026-07-04)
# Deezer isimle artist araması yanlış sanatçı buluyor (Motive→Harp, Ceza→Brezilya).
# Track araması doğru artist.id'yi zaten görüyor → onu taşı, isimle arama yok.
# ─────────────────────────────────────────────────────────────────────
def test_track_anchor_returns_artist_id():
    """get_deezer_track_scored_with_anchor artık (scored, ad, id) döndürür."""
    from app.services.deezer_genre import get_deezer_track_scored_with_anchor

    class _Resp:
        def __init__(self, p): self._p = p
        def raise_for_status(self): pass
        def json(self): return self._p

    class _Http:
        def get(self, url, timeout=10):
            if "search" in url:
                return _Resp({"data": [{
                    "artist": {"id": 72196, "name": "Motive"},
                    "album": {"id": 500},
                }]})
            return _Resp({"genres": {"data": [{"name": "Rap/Hip Hop"}]}})

    scored, anchor, anchor_id = get_deezer_track_scored_with_anchor("Motive", "Makaveli", _Http())
    assert anchor == "Motive"
    assert anchor_id == 72196
    assert ("Rap/Hip Hop", 0) in scored


def test_track_anchor_no_result_returns_none_id():
    """Track bulunamazsa id de None."""
    from app.services.deezer_genre import get_deezer_track_scored_with_anchor

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"data": []}

    class _Http:
        def get(self, url, timeout=10): return _Resp()

    scored, anchor, anchor_id = get_deezer_track_scored_with_anchor("X", "Y", _Http())
    assert scored == []
    assert anchor is None
    assert anchor_id is None


def test_artist_genres_by_id_uses_albums_directly():
    """get_deezer_artist_genres_by_id: isimle arama YAPMAZ, doğrudan /artist/{id}/albums."""
    from app.services.deezer_genre import get_deezer_artist_genres_by_id

    albums = [{"genre_id": 116} for _ in range(5)] + [{"genre_id": 464}]

    class _Resp:
        def __init__(self, p): self._p = p
        def raise_for_status(self): pass
        def json(self): return self._p

    class _Http:
        def __init__(self): self.urls = []
        def get(self, url, timeout=10):
            self.urls.append(url)
            if "/albums" in url:
                return _Resp({"data": albums})
            return _Resp({})

    http = _Http()
    scored = get_deezer_artist_genres_by_id(72196, http)
    assert scored[0] == ("Rap/Hip Hop", 5)
    assert not any("/search" in u for u in http.urls)   # isimle arama yok
    assert any("/artist/72196/albums" in u for u in http.urls)


def test_artist_genres_by_id_none_returns_empty():
    """id yoksa boş liste, HTTP çağrısı yok."""
    from app.services.deezer_genre import get_deezer_artist_genres_by_id

    class _Http:
        def get(self, url, timeout=10):
            raise AssertionError("id yokken HTTP çağrılmamalı")

    assert get_deezer_artist_genres_by_id(None, _Http()) == []


def test_artist_genres_by_id_http_429_raises_ratelimit():
    """/albums HTTP 429 → RateLimitError (sessizce [] DÖNMEZ). Aksi halde canlıda
    profil MB'ye düşüp yanlış tür alıyordu (Motive→elektronik, 2026-07-04)."""
    import httpx
    import pytest
    from app.services.deezer_genre import get_deezer_artist_genres_by_id
    from app.services.genre_errors import RateLimitError

    class _Resp:
        status_code = 429
        headers = {"Retry-After": "20"}
        def raise_for_status(self):
            raise httpx.HTTPStatusError("429", request=None, response=self)
        def json(self): return {}

    class _Http:
        def get(self, url, timeout=10): return _Resp()

    with pytest.raises(RateLimitError) as exc:
        get_deezer_artist_genres_by_id(72196, _Http())
    assert exc.value.provider == "deezer"
    assert exc.value.retry_after == 20.0


def test_artist_genres_by_id_json_quota_error_raises_ratelimit():
    """Deezer bazen HTTP 200 + JSON {"error": {"code": 4}} (quota) döner —
    raise_for_status geçer ama veri yok. Bu da RateLimitError olmalı."""
    import pytest
    from app.services.deezer_genre import get_deezer_artist_genres_by_id
    from app.services.genre_errors import RateLimitError

    class _Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"error": {"code": 4, "message": "Quota limit exceeded"}}

    class _Http:
        def get(self, url, timeout=10): return _Resp()

    with pytest.raises(RateLimitError) as exc:
        get_deezer_artist_genres_by_id(72196, _Http())
    assert exc.value.provider == "deezer"

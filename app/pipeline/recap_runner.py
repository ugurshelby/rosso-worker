"""Recap payload üretimi — v2 (FAZ R3, 2026-07-20).

Efendim'in kararları (docs/plans/01-recap-mimarisi-sifirdan.md §4):
  · Donmuş payload — kullanıcı sayfayı açtığında hesap YAPILMAZ, DB'den okunur
  · Tetik: cron (bu dosya)
  · ⚠ İKİ ZORUNLU KORUMA (eski model tam bu iki sınıftan veri kaybetti):

    (a) **Kısmi güncelleme.** `upsert_recap_partial` payload'ı `jsonb ||` ile
        birleştirir, BAŞTAN KURMAZ. Eski `build_recap` payload'ı sıfırdan
        kuruyordu → yalnız onu çalıştırmak `year_extras`'ı sessizce siliyordu
        (canlı olay, 2026-07-20). Yeni imzada silmek için açıkça null yazmak
        gerekir; kaza ile silinemez.

    (b) **Satır sayısı nöbetçisi.** Tur sonunda `audit_recap_coverage` çağrılır;
        beklenen dönem sayısı ile yazılan tutmuyorsa outcome `partial` olur ve
        eksik etiketler loglanır. Eski sistemde fonksiyon yanlış rolle
        çağrılınca guard CTE'si hata vermeden 0 satır dönüyordu — cron
        "success" yazıyordu ama hiçbir şey yazılmamıştı. Bu nöbetçi o
        sessizliği kırar (CLAUDE.md §1.5: "success ≠ doğru sonuç").

Kapsam (2026-08-11, Kart 8/9 kaldırıldıktan sonra): cover · manifesto ·
top_artists · top_tracks · discovery · streak · peak_day payload'ı üretilir.
Her kart KENDİ anahtarını yazar; verisi yoksa anahtar HİÇ yazılmaz (boş kart
basılmaz, ve kısmi güncelleme sayesinde eski değer korunur).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger("rosso.worker.recap_runner")


def is_period_completed(period_end: str, now: datetime | None = None) -> bool:
    """Dönem TAMAMLANDI mı? (KATMAN 6 guard, 2026-07-25)

    ⚠ Canlı olay (2026-07-24): cari dönem (henüz bitmemiş ay/yıl) için recap
    üretiliyordu → 34 hatalı "yarım dönem" kaydı oluştu, elle silindi ama guard
    KODDA YOKTU → cron yeniden üretecekti. Bu nöbetçi cari dönemi atlar.

    Mantık: `period_end` (dönemin son günü, ör. aylık ay-sonu, yıllık 31 Aralık)
    bugünden ÖNCEYSE dönem bitmiştir. Bugün hâlâ dönemin içindeyse (period_end
    gelecekte veya bugün) dönem cari → recap üretilmez. Bu, bug raporundaki
    `period_year<cur OR (==cur AND period_month<cur)` kuralının period_end ile
    ifade edilmiş, ay/yıl ayrımı gerektirmeyen sağlam hâlidir.
    """
    now = now or datetime.now(timezone.utc)
    try:
        end = datetime.fromisoformat(f"{period_end}T23:59:59+00:00")
    except (ValueError, TypeError):
        # Tarih ayrıştırılamıyorsa güvenli taraf: üretme (cari say).
        return False
    return end < now


def build_cover_payload(period_type: str, period_label: str) -> dict[str, Any]:
    """Kart 1 (Kapak) payload'ı.

    Kapak kartı ham veri istemez — yıl/etiket zaten period bilgisinde. Sanat
    görseli seçimi UI tarafında deterministik hash ile yapılır
    (`lib/recap/cover-art.ts`), payload'a yazılmaz: görsel havuzu değişirse
    donmuş payload eski dosyaya işaret etmesin.
    """
    return {
        "cover": {
            "issue_label": "ANNUAL ARCHIVE" if period_type == "year" else "MONTHLY ISSUE",
            "period_label": period_label,
        }
    }


def build_manifesto_payload(
    client: Any, user_id: str, start: str, end: str
) -> dict[str, Any]:
    """Kart 2 (Manifesto Poster) — dakika / şarkı / sanatçı / baskın tür.

    Efendim'in tarifi: dört dev satır, kutucuk YOK, ham verinin adı yazılır
    ("29,367 MINS LOGGED") — algoritmik yorum içermez.

    Veri yoksa anahtar HİÇ yazılmaz (None döner) — kısmi güncelleme sayesinde
    eski değer korunur, boş kart basılmaz.
    """
    res = client.rpc(
        "recap_listening_summary",
        {"p_user_id": user_id, "p_from": start, "p_to": end},
    ).execute()
    row = (res.data or [{}])[0]
    total_ms = int(row.get("total_ms") or 0)
    if total_ms == 0:
        return {}

    genre_res = client.rpc(
        "recap_dominant_genre",
        {"p_user_id": user_id, "p_from": start, "p_to": end},
    ).execute()
    genre_row = (genre_res.data or [{}])[0] if genre_res.data else {}

    # A6 — araba dinlemesi TEK SATIR (plan P3: "ayrı ekran YOK").
    # Manifesto'nun içine iliştirilir; veri yoksa anahtar HİÇ yazılmaz
    # ("veri yoksa özellik yok" — boş satır basılmaz).
    car_hours: float | None = None
    car_sessions: int | None = None
    try:
        car_res = client.rpc(
            "car_listening_summary",
            {"p_user_id": user_id, "p_from": start, "p_to": end},
        ).execute()
        car_row = (car_res.data or [{}])[0] if car_res.data else {}
        if car_row and (car_row.get("session_count") or 0) > 0:
            car_sessions = int(car_row["session_count"])
            car_hours = float(car_row.get("total_hours") or 0)
    except Exception:  # noqa: BLE001
        # Araba verisi bir SÜS — yoksa recap yine tam basılır. Bu çağrının
        # hatası tüm manifesto kartını düşürmemeli.
        logger.warning("car_listening_summary okunamadi user=%s", user_id)

    manifesto: dict[str, Any] = {
        "minutes": total_ms // 60000,
        "tracks": int(row.get("total_tracks") or 0),
        "artists": int(row.get("total_artists") or 0),
        "dominant_genre": genre_row.get("genre_name"),
    }
    if car_sessions:
        manifesto["car_hours"] = round(car_hours or 0, 1)
        manifesto["car_sessions"] = car_sessions

    return {"manifesto": manifesto}


def build_top_artists_payload(
    client: Any, user_id: str, start: str, end: str
) -> dict[str, Any]:
    """Kart 3 (Konser Posteri) — dikey 5 sütun, her sütunda sanatçı görseli.

    Görsel URL'i payload'a YAZILIR (kapak sanatının aksine): sanatçı görselleri
    kalıcı depolamada, dosya adı değişmiyor; UI'nin ayrıca sorgu atmasını
    önler. Görseli olmayan sanatçı yine listede kalır (UI fallback gösterir).
    """
    res = client.rpc(
        "recap_top_artists",
        {"p_user_id": user_id, "p_from": start, "p_to": end, "p_limit": 5},
    ).execute()
    rows = res.data or []
    if not rows:
        return {}

    names = [r["artist_name"] for r in rows if r.get("artist_name")]
    images: dict[str, str] = {}
    if names:
        try:
            img = (
                client.table("artists")
                .select("name,image_url")
                .in_("name", names)
                .execute()
            )
            images = {
                a["name"]: a["image_url"]
                for a in (img.data or [])
                if a.get("image_url")
            }
        except Exception:  # noqa: BLE001
            logger.warning("sanatçı görselleri alınamadı user=%s", user_id)

    return {
        "top_artists": [
            {
                "name": r["artist_name"],
                "plays": int(r.get("play_count") or 0),
                "image_url": images.get(r["artist_name"]),
            }
            for r in rows
            if r.get("artist_name")
        ]
    }


def build_top_tracks_payload(
    client: Any, user_id: str, start: str, end: str
) -> dict[str, Any]:
    """Kart 4 (Plak Rafı) — yatay 5 satır, albüm kapağı arkada.

    ⚠ `recap_top_tracks` görsel DÖNDÜRMEZ (canlı ölçüm: dönüşü track_id, title,
    artist_name, play_count, total_ms, skip_count). Kapak `tracks.image_url`'den
    track_id ile ayrıca çekilir — varsayıp `r["image_url"]` okumak sessizce
    None üretirdi.
    """
    res = client.rpc(
        "recap_top_tracks",
        {"p_user_id": user_id, "p_from": start, "p_to": end, "p_limit": 5},
    ).execute()
    rows = res.data or []
    if not rows:
        return {}

    track_ids = [r["track_id"] for r in rows if r.get("track_id")]
    covers: dict[str, str] = {}
    if track_ids:
        try:
            img = (
                client.table("tracks")
                .select("id,image_url")
                .in_("id", track_ids)
                .execute()
            )
            covers = {
                t["id"]: t["image_url"]
                for t in (img.data or [])
                if t.get("image_url")
            }
        except Exception:  # noqa: BLE001
            logger.warning("track kapakları alınamadı user=%s", user_id)

    return {
        "top_tracks": [
            {
                "title": r["title"],
                "artist": r.get("artist_name"),
                "plays": int(r.get("play_count") or 0),
                "image_url": covers.get(r.get("track_id")),
            }
            for r in rows
            if r.get("title")
        ]
    }


def _month_end_iso(month_start: str) -> str:
    """'YYYY-MM-DD' (ayın 1'i) → o ayın SON anı, ISO.

    Ay uzunluğu 28/29/30/31 değişir; sabit 30 gün eklemek Şubat'ta bir sonraki
    aya taşar, Ocak'ta bir gün eksik bırakırdı. Bir sonraki ayın 1'ini bulup
    bir saniye geri gidiyoruz.
    """
    y, m, _ = (int(p) for p in month_start.split("-"))
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return (
        datetime(ny, nm, 1, tzinfo=timezone.utc) - timedelta(seconds=1)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_discovery_payload(
    client: Any, user_id: str, start: str, end: str
) -> dict[str, Any]:
    """Kart 5 (The Discovery Archive) — en çok keşif yapılan ay.

    RPC ayları keşif sayısına göre SIRALI döndürür; ilk satır zirve aydır.
    Kart yalnız o ayı gösteriyor, ama sıralı liste ileride "keşif ritmi"
    için de kullanılabilir diye ilk 12 ay RPC'den geliyor.
    """
    res = client.rpc(
        "recap_discovery_by_month",
        {"p_user_id": user_id, "p_from": start, "p_to": end},
    ).execute()
    rows = res.data or []
    if not rows:
        return {}

    top = rows[0]
    if not top.get("month_start"):
        return {}

    payload: dict[str, Any] = {
        "month_start": top["month_start"],
        "new_artists": int(top.get("new_artists") or 0),
        "new_tracks": int(top.get("new_tracks") or 0),
    }

    # Kartın iki 1:1 karesi için o AYIN en çok çalınan şarkısı/sanatçısı.
    # Mevcut recap_top_* RPC'leri dönem parametreli — ayın sınırlarını verip
    # yeniden kullanıyoruz (yeni RPC yazmaya gerek yok).
    ay_bas = f"{top['month_start']}T00:00:00Z"
    ay_son = _month_end_iso(top["month_start"])
    try:
        t = client.rpc(
            "recap_top_tracks",
            {"p_user_id": user_id, "p_from": ay_bas, "p_to": ay_son, "p_limit": 1},
        ).execute()
        trow = (t.data or [None])[0]
        if trow and trow.get("title"):
            cover = None
            if trow.get("track_id"):
                img = (
                    client.table("tracks")
                    .select("image_url")
                    .eq("id", trow["track_id"])
                    .limit(1)
                    .execute()
                )
                cover = ((img.data or [{}])[0] or {}).get("image_url")
            payload["top_track"] = {
                "title": trow["title"],
                "artist": trow.get("artist_name"),
                "image_url": cover,
            }

        a = client.rpc(
            "recap_top_artists",
            {"p_user_id": user_id, "p_from": ay_bas, "p_to": ay_son, "p_limit": 1},
        ).execute()
        arow = (a.data or [None])[0]
        if arow and arow.get("artist_name"):
            img = (
                client.table("artists")
                .select("image_url")
                .eq("name", arow["artist_name"])
                .limit(1)
                .execute()
            )
            payload["top_artist"] = {
                "name": arow["artist_name"],
                "image_url": ((img.data or [{}])[0] or {}).get("image_url"),
            }
    except Exception:  # noqa: BLE001
        # Görseller kartın ZORUNLU parçası değil — sayılar yine basılır.
        logger.warning("keşif ayı görselleri alınamadı user=%s", user_id)

    return {"discovery": payload}


def build_streak_payload(
    client: Any, user_id: str, start: str, end: str
) -> dict[str, Any]:
    """Kart 6 (The Unbroken Chain) — en uzun ardışık dinleme serisi.

    ⚠ Mevcut `user_streaks` RPC'si KULLANILAMAZ: global (dönem parametresi
    yok) ve tarih aralığı döndürmüyor — kartın "OCT 12 — FEB 11" etiketi
    için başlangıç/bitiş şart. Bu yüzden 0126'da dönem-parametreli
    `recap_longest_streak` yazıldı.

    Tek günlük "seri" seri değildir; kart basılmaz (anahtar hiç yazılmaz).
    """
    res = client.rpc(
        "recap_longest_streak",
        {"p_user_id": user_id, "p_from": start, "p_to": end},
    ).execute()
    rows = res.data or []
    if not rows:
        return {}

    row = rows[0]
    days = int(row.get("streak_days") or 0)
    if days < 2 or not row.get("streak_start"):
        return {}

    return {
        "streak": {
            "days": days,
            "start": row["streak_start"],
            "end": row["streak_end"],
        }
    }


def build_peak_day_payload(
    client: Any, user_id: str, start: str, end: str
) -> dict[str, Any]:
    """Kart 7 (The Radar Scan) — en yoğun gün + o günün ilk 3 şarkısı.

    RPC gün bilgisini HER satırda tekrarlar (tek sorguda iki iş yapıyor):
    ilk satırdan gün/süre alınır, tüm satırlardan şarkı listesi kurulur.
    Şarkısı olmayan satır (left join) atlanır.
    """
    res = client.rpc(
        "recap_peak_day",
        {"p_user_id": user_id, "p_from": start, "p_to": end},
    ).execute()
    rows = res.data or []
    if not rows or not rows[0].get("day"):
        return {}

    head = rows[0]
    tracks = [
        {
            "title": r["title"],
            "artist": r.get("artist"),
            "plays": int(r.get("plays") or 0),
            "image_url": r.get("image_url"),
        }
        for r in rows
        if r.get("title")
    ]

    return {
        "peak_day": {
            "day": head["day"],
            "minutes": round(int(head.get("total_ms") or 0) / 60000),
            "plays": int(head.get("play_count") or 0),
            "tracks": tracks,
        }
    }


def build_obsession_payload(
    client: Any, user_id: str, start: str, end: str
) -> dict[str, Any]:
    """A13.5 (Takıntı AHA kartı) — tek şarkıya saplanma anı.

    `recap_obsession` (0193) YOĞUNLAŞMA ölçer: en yoğun 30 günlük pencerede
    ≥15 çalma şartı var. Eşik altındaysa RPC hiç satır döndürmez → kart yok.

    ⚠ 0192'nin ilk hâli en çok ÇALINAN şarkıyı seçiyordu ve 342 güne yayılmış
    bir dinlemeyi "takıntı" diye sunuyordu. Yayılmış dinleme sadakattir.
    """
    try:
        res = client.rpc(
            "recap_obsession",
            {"p_user_id": user_id, "p_from": start, "p_to": end},
        ).execute()
    except Exception:
        logger.warning("recap_obsession okunamadi user=%s donem=%s", user_id, start)
        return {}

    rows = res.data or []
    if not rows:
        return {}

    r = rows[0] or {}
    if not r.get("title"):
        return {}

    return {
        "obsession": {
            "title": r["title"],
            "artist": r.get("artist") or "",
            "plays": int(r.get("plays") or 0),
            "hours": float(r.get("hours") or 0),
            "share_pct": float(r.get("share_pct") or 0),
            "span_days": int(r.get("span_days") or 0),
            "peak_window_plays": int(r.get("peak_window_plays") or 0),
            "peak_window_start": r.get("peak_window_start"),
        }
    }


def build_top_albums_payload(
    client: Any, user_id: str, start: str, end: str
) -> dict[str, Any]:
    """A13.6 (Top albümler) — yalnız GERÇEK albüm dinlemeleri.

    `recap_top_albums` (0192) aynı albümden ≥3 farklı şarkı şartı koyar;
    aksi hâlde single'lar albüm gibi listeleniyordu (ölçüldü).
    """
    try:
        res = client.rpc(
            "recap_top_albums",
            {"p_user_id": user_id, "p_from": start, "p_to": end, "p_limit": 5},
        ).execute()
    except Exception:
        logger.warning("recap_top_albums okunamadi user=%s donem=%s", user_id, start)
        return {}

    rows = res.data or []
    if not rows:
        return {}

    albums = [
        {
            "album": r.get("album") or "",
            "artist": r.get("artist") or "",
            "distinct_tracks": int(r.get("distinct_tracks") or 0),
            "plays": int(r.get("plays") or 0),
            "hours": float(r.get("hours") or 0),
        }
        for r in rows
        if r.get("album")
    ]
    if not albums:
        return {}

    return {"top_albums": albums}


def build_recap_extras_payload(
    client: Any, user_id: str, start: str, end: str
) -> dict[str, Any]:
    """A13.2 + A13.3 + A13.7 + A13.8 — manifesto/vurgu satırlarını besleyen sayılar.

    Dördü de AYRI KART DEĞİL:
      A13.2 dönem keşif toplamı  → yıllık özet satırı
      A13.3 #1 sanatçı/şarkı     → vurgu (mevcut top-5 listelerinden farklı:
                                    tek zirve + gerçek dinleme SAATİ)
      A13.7 tür çeşitliliği      → manifesto satırı (plan açıkça böyle diyor)
      A13.8 dinleme yaşı         → satır (kart DEĞİL — plan açıkça böyle diyor)

    Hepsi tek bir `extras` anahtarında toplanır: her biri için ayrı payload
    dalı açmak, kart olmayan şeylere kart muamelesi yapmak olurdu.
    """
    out: dict[str, Any] = {}

    try:
        res = client.rpc(
            "recap_discovery_total",
            {"p_user_id": user_id, "p_from": start, "p_to": end},
        ).execute()
        r = (res.data or [{}])[0] or {}
        if r.get("total_tracks"):
            out["discovery_total"] = {
                "new_artists": int(r.get("new_artists") or 0),
                "new_tracks": int(r.get("new_tracks") or 0),
                "total_artists": int(r.get("total_artists") or 0),
                "total_tracks": int(r.get("total_tracks") or 0),
                "discovery_rate": float(r["discovery_rate"])
                if r.get("discovery_rate") is not None
                else None,
            }
    except Exception:
        logger.warning("recap_discovery_total okunamadi user=%s", user_id)

    try:
        res = client.rpc(
            "recap_number_one",
            {"p_user_id": user_id, "p_from": start, "p_to": end},
        ).execute()
        r = (res.data or [{}])[0] or {}
        if r.get("artist_name") or r.get("track_title"):
            track_img = r.get("track_image_url")
            artist_img = r.get("artist_image_url")

            if not track_img and r.get("track_title"):
                try:
                    t_res = (
                        client.from_("tracks")
                        .select("image_url")
                        .ilike("title", r["track_title"])
                        .not_.is_("image_url", "null")
                        .limit(1)
                        .execute()
                    )
                    if t_res.data:
                        track_img = t_res.data[0].get("image_url")
                except Exception:
                    pass

            if not artist_img and r.get("artist_name"):
                try:
                    a_res = (
                        client.from_("artists")
                        .select("image_url")
                        .ilike("name", r["artist_name"])
                        .not_.is_("image_url", "null")
                        .limit(1)
                        .execute()
                    )
                    if a_res.data:
                        artist_img = a_res.data[0].get("image_url")
                except Exception:
                    pass

            out["number_one"] = {
                "artist_name": r.get("artist_name"),
                "artist_hours": float(r["artist_hours"]) if r.get("artist_hours") else None,
                "artist_plays": int(r.get("artist_plays") or 0),
                "artist_image_url": artist_img,
                "track_title": r.get("track_title"),
                "track_artist": r.get("track_artist"),
                "track_hours": float(r["track_hours"]) if r.get("track_hours") else None,
                "track_plays": int(r.get("track_plays") or 0),
                "track_image_url": track_img,
            }
    except Exception:
        logger.warning("recap_number_one okunamadi user=%s", user_id)

    try:
        res = client.rpc(
            "recap_genre_variety",
            {"p_user_id": user_id, "p_from": start, "p_to": end},
        ).execute()
        r = (res.data or [{}])[0] or {}
        if r.get("genre_count"):
            out["genre_variety"] = {
                "genre_count": int(r.get("genre_count") or 0),
                "top_genre": r.get("top_genre"),
                "top_genre_pct": float(r["top_genre_pct"])
                if r.get("top_genre_pct") is not None
                else None,
            }
    except Exception:
        logger.warning("recap_genre_variety okunamadi user=%s", user_id)

    # A13.8 — dinleme yaşı. RPC ön koşulları sağlamazsa (doğum tarihi yok,
    # release_year kapsaması <%50, örneklem <30) BOŞ döner; o zaman anahtar
    # hiç yazılmaz. Uydurma sayı basmaktansa satırı hiç göstermemek doğru.
    try:
        res = client.rpc(
            "recap_listening_age",
            {"p_user_id": user_id, "p_from": start, "p_to": end},
        ).execute()
        r = (res.data or [{}])[0] or {}
        if r.get("listening_age") is not None:
            out["listening_age"] = {
                "age": int(r["listening_age"]),
                "user_age": int(r.get("user_age") or 0),
                "avg_release_year": float(r["avg_release_year"])
                if r.get("avg_release_year") is not None
                else None,
                "coverage_pct": float(r["coverage_pct"])
                if r.get("coverage_pct") is not None
                else None,
                "sample_size": int(r.get("sample_size") or 0),
            }
    except Exception:
        logger.warning("recap_listening_age okunamadi user=%s", user_id)

    return {"extras": out} if out else {}


def refresh_user_recaps(client: Any, user_id: str) -> dict[str, Any]:
    """Bir kullanıcının tüm dönemleri için recap payload'ını tazeler.

    Dönem listesi DB tarafında üretilir (`recap_periods_with_data`) — REST'in
    ~1000 satır kırpması buraya bulaşamaz (CLAUDE.md §1.65).
    """
    res = client.rpc("recap_periods_with_data", {"p_user_id": user_id}).execute()
    periods = res.data or []
    if not periods:
        return {"periods": 0, "written": 0, "errors": 0}

    written = 0
    errors = 0
    skipped = 0
    for p in periods:
        # KATMAN 6 guard: cari (henüz bitmemiş) dönem için recap ÜRETME.
        # 2026-07-24 canlı olayı: yarım dönem kayıtları oluşup silinmişti,
        # guard yoktu → cron yeniden üretiyordu. Artık atlanır.
        if not is_period_completed(p["period_end"]):
            skipped += 1
            continue
        try:
            # Dönem sınırları RPC'lere timestamptz olarak gider (period_end
            # tarihin KENDİSİ dahil olsun diye gün sonuna çekilir).
            start = f"{p['period_start']}T00:00:00Z"
            end = f"{p['period_end']}T23:59:59Z"

            payload: dict[str, Any] = {}
            payload.update(build_cover_payload(p["period_type"], p["period_label"]))
            payload.update(build_manifesto_payload(client, user_id, start, end))
            payload.update(build_top_artists_payload(client, user_id, start, end))
            payload.update(build_top_tracks_payload(client, user_id, start, end))
            payload.update(build_discovery_payload(client, user_id, start, end))
            payload.update(build_streak_payload(client, user_id, start, end))
            payload.update(build_peak_day_payload(client, user_id, start, end))
            # Kart 8 (soundscape) ve Kart 9 (A13.1 duygu profili) 2026-08-11'de
            # kaldırıldı — Efendim: "bu ekranları görmek bile istemiyorum,
            # hem web hem app heryerden silinsin." `recap_hourly_distribution`
            # ve `mood_profile` RPC'leri artık burada çağrılmıyor.
            # A13.5 — takıntı (0193). Yoğunlaşma eşiği altındaysa boş döner.
            payload.update(build_obsession_payload(client, user_id, start, end))
            # A13.6 — top albümler (0192). ≥3 farklı şarkı şartı RPC'de.
            payload.update(build_top_albums_payload(client, user_id, start, end))
            # A13.2 + A13.3 + A13.7 — kart değil, satır besleyen sayılar.
            payload.update(build_recap_extras_payload(client, user_id, start, end))

            client.rpc(
                "upsert_recap_partial",
                {
                    "p_user_id": user_id,
                    "p_period_type": p["period_type"],
                    "p_period_label": p["period_label"],
                    "p_period_start": p["period_start"],
                    "p_period_end": p["period_end"],
                    "p_payload": payload,
                },
            ).execute()
            written += 1
        except Exception as exc:  # noqa: BLE001
            errors += 1
            logger.warning(
                "recap yazımı başarısız user=%s period=%s: %s",
                user_id, p.get("period_label"), str(exc)[:200],
            )

    return {
        "periods": len(periods),
        "written": written,
        "errors": errors,
        "skipped_current": skipped,
    }


def run_recap_refresh(client: Any) -> dict[str, Any]:
    """play_events'i olan her kullanıcı için recap payload'larını tazeler.

    Bir kullanıcının hatası diğerlerini durdurmaz (izole). Tur sonunda
    nöbetçi (koruma b) her kullanıcı için kapsamı doğrular.
    """
    # Tekil kullanıcı listesi DB tarafında (CLAUDE.md §1.65 — `.limit()` + set()
    # yolu 253k satırlık play_events'te yalnız EN ESKİ kullanıcıları görüyordu,
    # Ferzan iki cron'dan da sessizce düşmüştü).
    # recap_real_user_ids (0129): yalnız GERÇEK kullanıcılar — test profillerine
    # recap üretilmez (Efendim 2026-07-24). taste_runner ise recap_user_ids'i
    # (test dahil) kullanmaya devam eder; test profilleri eşleşme girdisidir.
    try:
        res = client.rpc("recap_real_user_ids", {}).execute()
        user_ids = sorted({u for u in (res.data or []) if u})
    except Exception as exc:  # noqa: BLE001
        # Sessizce eski yola düşmek yerine GÖRÜNÜR hata: bu RPC'nin kaybı
        # tam da yukarıdaki kırpma bug'ını geri getirir.
        logger.exception("recap_real_user_ids RPC başarısız — tur iptal")
        return {"outcome": "error", "users_processed": 0, "errors": 1,
                "error": str(exc)[:300]}

    if not user_ids:
        return {"outcome": "empty", "users_processed": 0, "errors": 0}

    processed = 0
    errors = 0
    written_total = 0
    incomplete: list[str] = []

    for uid in user_ids:
        try:
            stats = refresh_user_recaps(client, uid)
            written_total += stats["written"]
            errors += stats["errors"]
            processed += 1
            # P0 (0127): "Tüm Zamanlar" özet cache'ini tazele. Ağır sorgu
            # (JOIN+COUNT DISTINCT, ~2,6sn) burada arka planda koşar — kullanıcı
            # dashboard açılışında beklemesin. Recap tazelemeyi bozmasın diye
            # ayrı try: cache hatası bir kullanıcının recap'ini düşürmemeli.
            try:
                client.rpc(
                    "refresh_listening_summary_cache", {"p_user_id": uid}
                ).execute()
            except Exception as cache_exc:  # noqa: BLE001
                logger.warning(
                    "listening_summary_cache tazeleme başarısız user=%s: %s",
                    uid, str(cache_exc)[:200],
                )
        except Exception as exc:  # noqa: BLE001
            errors += 1
            logger.warning("recap refresh başarısız user=%s: %s", uid, str(exc)[:200])
            continue

        # ⚠ KORUMA (b): nöbetçi. "Yazdım" demek yetmez — gerçekten yazıldı mı?
        try:
            audit = client.rpc("audit_recap_coverage", {"p_user_id": uid}).execute()
            row = (audit.data or [{}])[0]
            missing = row.get("missing_labels") or []
            if missing:
                incomplete.append(uid)
                logger.error(
                    "recap kapsamı EKSİK user=%s beklenen=%s yazılan=%s eksik=%s",
                    uid, row.get("expected_periods"), row.get("stored_periods"),
                    missing[:5],
                )
        except Exception:  # noqa: BLE001
            logger.warning("audit_recap_coverage başarısız user=%s", uid)

    if processed == 0 and errors > 0:
        outcome = "error"
    elif errors > 0 or incomplete:
        outcome = "partial"
    else:
        outcome = "success"

    return {
        "outcome": outcome,
        "users_processed": processed,
        "recaps_written": written_total,
        "errors": errors,
        "incomplete_users": len(incomplete),
    }

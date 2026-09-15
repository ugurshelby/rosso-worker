# Railway Kurulum — Worker (3 Servis)

> Mimari: `worker/app/cron/fast.py` + `worker/app/cron/nightly.py`
> Eski 16 ayrı cron servisi yerine **3 Railway servisi** (Free plan).

Hepsi aynı repo `worker/` dizininden deploy edilir.

---

## Ortak Railway ayarları (3 servis için aynı)

| Alan | Değer |
|------|-------|
| **Root Directory** | `worker` |
| **Builder** | Dockerfile (varsayılan `worker/Dockerfile`) |
| **Watch paths** | `worker/**` (repo kökünden deploy ediyorsanız) |

**Ortam değişkenleri** (Health + iki cron için aynı set):

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `TOKEN_ENCRYPTION_KEY` (Spotify token şifreleme — recently-played + playlist için)
- `LAST_FM_API_KEY` (enrichment)
- Diğer worker env'leri: `.env.local` ile Railway paneli **eşit** olmalı

---

## 1. Health (web) — sürekli ayakta

| Alan | Değer |
|------|-------|
| **Servis adı** | `rosso-worker-health` (veya mevcut Health servisi) |
| **Servis tipi** | Web |
| **Start command** | `uvicorn app.health:app --host 0.0.0.0 --port $PORT --no-access-log --log-level warning` |
| **Healthcheck path** | `/health` |
| **Cron** | Yok |

İzleme + DB hazır kontrolü. İş yapmaz.

---

## 2. worker-fast — sık cron

| Alan | Değer |
|------|-------|
| **Servis adı** | `rosso-worker-fast` |
| **Servis tipi** | Cron Job |
| **Cron schedule** | `*/5 * * * *` (her 5 dakika) |
| **Start command** | `python -m app.cron.fast` |

**Her turda sırayla:**

1. **export** — kuyruk varsa 5 dk bütçe içinde birden fazla ZIP
2. **spotify_recently_played** — son dinlenenler
3. **enrichment** — 1 tür batch (~240 sn bütçe)
4. **isrc_backfill** — 1 ISRC batch
5. **playlist_refresh** — yalnız son başarılı turdan ≥12 saat geçtiyse

Daha sık ZIP işleme istersen schedule `*/2 * * * *` yapılabilir.

---

## 3. worker-nightly — gece cron

| Alan | Değer |
|------|-------|
| **Servis adı** | `rosso-worker-nightly` |
| **Servis tipi** | Cron Job |
| **Cron schedule** | `0 3 * * *` (günde 1, 03:00 UTC = 06:00 TR) |
| **Start command** | `python -m app.cron.nightly` |

**Her gece sırayla:**

1. **taste_refresh** — yalnız **Pazartesi** (UTC)
2. **journey_pkg** → **taste_pkg** → **pattern_pkg** → **stats_pkg** → **period_pkg**
3. **mood_pkg** (+ içindeki mood_weekly_sync)
4. **recap_refresh**
5. **match_batch** — kişisel Rosso'da atlanır (`reason=personal`)
6. **auto_playlist** (iç `last_run_at` kapıları aylık üretimi yönetir)
7. **account_purge** (en sonda — yıkıcı)

---

## Eski servisler — kaldır

Health dışındaki tüm ayrı cron servisleri silinebilir:

- Export, Enrichment, Spotify recently-played, ISRC, Playlist Tazeleme
- journey-pkg, taste-pkg, pattern-pkg, stats-pkg, period-pkg, mood-pkg
- Taste Refresh, Recap Refresh, Match Batch, Auto Playlist, Otomatik purge
- Nightly Sync (zaten ölü kod)

Tekil cron modülleri (`app/cron/export.py` vb.) **silinmedi** — test ve acil elle çalıştırma için duruyor.

---

## Doğrulama (deploy sonrası)

Railway loglarında `worker_fast` / `worker_nightly` satırlarını ara.

`pipeline_runs` tablosunda her `run_type` için turlar akmaya devam etmeli:

| run_type | Beklenen kaynak |
|----------|-----------------|
| export | worker-fast |
| spotify_recently_played | worker-fast |
| enrichment | worker-fast |
| isrc_backfill | worker-fast |
| playlist_refresh | worker-fast (≤2/gün) |
| journey_pkg … mood_pkg | worker-nightly |
| taste_refresh | worker-nightly (Pazartesi) |
| recap_refresh | worker-nightly |
| match_batch | atlanır (kişisel) |
| auto_playlist | worker-nightly |
| account_purge | worker-nightly |

---

## Tarihsel notlar

- YT Music cron'ları kaldırıldı (saf Spotify, plan 07).
- Kapak/sanatçı kör dolgu cron'ları emekli (2026-08-05).
- Nightly sync kaldırıldı (2026-08-26).

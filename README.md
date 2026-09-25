# Rosso Worker

Ağır/uzun işler için izole Python servisi. Ana uygulama (Next.js + Supabase Edge)
hafif işleri yapar; bu worker **Deno/Edge'in kaldıramadığı** işleri üstlenir:

- **Spotify ZIP export işleme** (500MB; `ijson` ile bellek-dostu streaming parse)
- **Track matching** (ISRC öncelikli, fuzzy fallback)
- **(Faz 5)** `ytmusicapi` servisleri — Python kütüphanesi, Deno'da çalışmaz

Worker çökerse çekirdek sistem ayakta kalır: `export_jobs` `queued` kalır,
worker dönünce kaldığı yerden işler.

## Mimari

```
TS upload route → Storage'a ZIP yaz + export_jobs kaydı + pgmq'ya mesaj
                                              │
                                              ▼
worker loop (main.py) ── pgmq poll ──► process_export
                                              │  parser + matcher (saf, test-edilebilir)
                                              ▼
                                        play_events (batch insert, service role)
                                              │
                                              ▼
                                  export_jobs güncelle → Realtime → UI
```

## Çalıştırma

```bash
cd worker
pip install -e ".[dev]"        # bağımlılıklar + test
cp .env.example .env           # SUPABASE_URL + SERVICE_ROLE_KEY doldur
uvicorn main:app --reload      # http://localhost:8000/health
pytest                         # saf fonksiyon testleri (DB gerektirmez)
```

## Deploy

Worker 7/24 açık değildir (sıfır maliyet mimarisi): ağır işler GitHub Actions'ta on-demand çalışır,
günlük işler Supabase `pg_cron` ile tetiklenir. `Dockerfile` gerekirse konteynerde çalıştırmak içindir. Env: `SUPABASE_URL`,
`SUPABASE_SERVICE_ROLE_KEY`. Health check: `GET /health`.

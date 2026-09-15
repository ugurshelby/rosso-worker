# Varsayılan Playlist Kapakları

Otomatik playlist cron'unun (`auto_playlist_generator.py`) Spotify'da oluşturduğu
playlist'lere yüklediği **tüm kullanıcılar için ortak** varsayılan kapaklar.

## Dosya düzeni (kod bunları bekler)

```
monthly/01.jpg   → Ocak playlist'leri
monthly/02.jpg   → Şubat
...
monthly/12.jpg   → Aralık
annual.jpg       → Yıllık playlist'ler
```

- Kod `_cover_path_for(rule_type, month)` ile seçer.
- Dosya yoksa kapak **atlanır** (playlist yine oluşur, mozaik kapak alır) — kod
  `is_file()` ile güvenle kontrol eder, hata vermez.

## Kısıtlar

- **Format:** JPEG (`.jpg`), Spotify `image/jpeg` bekliyor.
- **Boyut:** ≤256KB (Spotify kapak yükleme limiti, base64 sonrası). Güvenli hedef
  ~160KB. Büyük görseller `sharp`/ImageMagick ile küçültülmeli (kare, ~640px).
- **Scope:** Yükleme `ugc-image-upload` Spotify izni ister. Kullanıcı bu izinle
  bağlanmamışsa 403 → kapak atlanır, playlist yine tam (yalnız loglanır).

## Not

Bunlar **jenerik/marka** kapaklardır (her ay aynı görsel, herkeste aynı). Kişisel
kapaklar (Uğur'un `ugur-spotify-personal/`) bununla karıştırılmaz — o ayrı, tek
seferlik bir işti.

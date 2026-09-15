"""Gerçek Spotify Account Data formatından örnek fixture'lar.

Kaynak: docs/platform-dev-docs/spotify zip/Spotify Account Data/
"""

# YourLibrary.json — top-level obje, tracks[] içinde {artist, album, track, uri}
# (gerçek: 2660 track)
YOUR_LIBRARY = {
    "tracks": [
        {"artist": "Ezhel", "album": "Müptezhel", "track": "Esrarengiz",
         "uri": "spotify:track:1UjF2lJacofdLCS6ipE95q"},
        {"artist": "Sera Savaş", "album": "Hani Çok Aşıktın", "track": "Hani Çok Aşıktın",
         "uri": "spotify:track:4WLiWGpG8uf0bd4C2NtyRj"},
    ],
    "albums": [], "shows": [], "episodes": [], "artists": [],
}

# Inferences.json — gerçek: 233 etiket. Müzik = "Interest | Music |" prefix.
# Diğerleri (Education, Hobbies, 1P_Custom, test-*) müzik DEĞİL.
INFERENCES = {
    "inferences": [
        "1P_Custom_ArtistAffinity_e5131e",
        "Interest | Music | Pop | Pop(1P)",
        "Interest | Education | Education | Education(1P)",
        "test-ingest-backend-yi2",
        "Interest | Music | Trap | Trap(1P)",
        "Interest | Music | Emo | Emo(1P)",
        "Interest | Hobbies & Interests | Musical Instruments | Musical Instruments(1P)",
    ],
}

# Wrapped2025.json — top-level obje, recap motoru için ham saklanır.
WRAPPED = {
    "topArtists": {"topArtistUris": ["spotify:artist:0PhqM7UAxtvWYi5j4MwxSl"],
                   "numUniqueArtists": 719},
    "topTracks": {"topTracks": []},
}

# YourSoundCapsule.json — top-level obje, ham saklanır.
SOUND_CAPSULE = {"stats": [], "highlights": []}

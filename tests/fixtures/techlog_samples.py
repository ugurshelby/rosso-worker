"""Gerçek Spotify Technical Log formatından örnek fixture'lar.

Kaynak: docs/platform-dev-docs/spotify zip/Spotify Technical Log Information/
Tüm değerler gerçek dosyaların ilk kayıtlarından birebir kopyalanmıştır
(kişisel olmayan: URI + timestamp + bool alanlar).
"""

# AddedToCollection.json — ilk 2 kayıt (gerçek: 198 kayıt, hepsi spotify:track:)
ADDED_TO_COLLECTION = [
    {"context_time": 1779346067718, "message_set": "collection",
     "message_item_uri": "spotify:track:0GabRJwqSIxLv3A139Lu6b",
     "message_context_uri": None, "message_item_data_json": None,
     "timestamp_utc": "2026-05-21T06:47:47.718Z"},
    {"context_time": 1779721827320, "message_set": "collection",
     "message_item_uri": "spotify:track:3RTOtVSpVcdC5JUhNXdifN",
     "message_context_uri": None, "message_item_data_json": None,
     "timestamp_utc": "2026-05-25T15:10:27.320Z"},
]

# RemovedFromCollection.json — gerçek format: track DEĞİL olan kayıtlar da var
# (ylpin→playlist, collection→album/track). Yalnızca spotify:track: işlenmeli.
REMOVED_FROM_COLLECTION = [
    {"context_time": 1781759783137, "message_set": "ylpin",
     "message_item_uri": "spotify:playlist:77qhEF3rUs3yKpfW765AuS",
     "message_context_uri": None, "message_item_data_json": None,
     "timestamp_utc": "2026-06-18T05:16:23.137Z"},
    {"context_time": 1781938866803, "message_set": "collection",
     "message_item_uri": "spotify:track:6IerS2EZLcEDn1OetmcDM6",
     "message_context_uri": None, "message_item_data_json": None,
     "timestamp_utc": "2026-06-20T07:01:06.803Z"},
    {"context_time": 1781431466962, "message_set": "collection",
     "message_item_uri": "spotify:album:1KiaSuwxsrULXxXuXlWtFY",
     "message_context_uri": None, "message_item_data_json": None,
     "timestamp_utc": "2026-06-14T13:24:26.962Z"},
]

# AddedToPlaylist.json — tekil message_item_uri (gerçek: 1801 kayıt)
ADDED_TO_PLAYLIST = [
    {"context_time": 1780763426587,
     "message_playlist_uri": "spotify:playlist:1Dx7G5XUqCybOHZzPrGGC1",
     "message_item_uri": "spotify:track:4hwZzYOnYRmYbMfWcxCivZ",
     "message_item_uri_kind": "track", "message_client_platform": None,
     "timestamp_utc": "2026-06-06T16:30:26.587Z"},
]

# AddToPlaylist.json — çoğul message_item_uris[] (gerçek: 166 kayıt)
ADD_TO_PLAYLIST = [
    {"context_time": 1780885299610, "context_conn_country": "TR",
     "context_os_name": "android",
     "message_item_uris": ["spotify:track:5TTGoX70AFrTvuEtqHK37S"],
     "message_playlist_uri": "spotify:playlist:0JvOz8ztPJm5n6tqUTrruy",
     "message_new_playlist": False,
     "timestamp_utc": "2026-06-08T02:21:39.610Z"},
]

# PlaylistCreated.json — gerçek: 7 kayıt
PLAYLIST_CREATED = [
    {"context_time": 1780334149482,
     "message_playlist_uri": "spotify:playlist:6SbTyambuhwcdaTzqFCxLu",
     "message_client_platform": None,
     "timestamp_utc": "2026-06-01T17:15:49.482Z"},
]

# CarDetectionEvent.json — gerçek: 3849 kayıt. connect(true)→disconnect(false)
CAR_DETECTION = [
    {"context_time": 1776943304732, "message_is_car_connected": True,
     "message_reason": "car", "timestamp_utc": "2026-04-23T11:21:44.732Z"},
    {"context_time": 1776974614137, "message_is_car_connected": False,
     "message_reason": "car_android_auto", "timestamp_utc": "2026-04-23T20:03:34.137Z"},
]

# DaylistGenerated.json — gerçek: 84 kayıt. Başlık message_playlist_title,
# gün-bölümü message_daypart ("morning"/"afternoon"/...) alanında.
DAYLIST_GENERATED = [
    {"context_time": 1771486860036, "message_country": "TR", "message_locale": "en",
     "message_day_of_week": "Thursday", "message_daypart": "morning",
     "message_playlist_title": "angst rock-ish thursday morning",
     "message_playlist_description": "Here's some angst, rock-ish, emo phase...",
     "message_tracks": ["spotify:track:0Tn14qJEmPOiCYd9To4tWz"],
     "timestamp_utc": "2026-02-19T07:41:00.036Z"},
]

# OnRepeatContents.json — gerçek: 56 kayıt
ON_REPEAT = [
    {"context_time": 1775788764240, "message_mix_id": "37i9dQZF1EpocFA4mFfzuX",
     "message_type": "ON_REPEAT",
     "message_contents": ["spotify:track:6RDZocqqVAW812vldECEfk"],
     "timestamp_utc": "2026-04-10T02:39:24.240Z"},
]

# HomeSectionResponse.json — gerçek: 14713 kayıt
HOME_SECTION = [
    {"context_time": 1773803846783,
     "message_content_uri": "spotify:section:0JQ5KskvsLe3uTeisPkjK7",
     "message_title": "Made for you", "message_subtitle": "",
     "message_feature_data": None,
     "timestamp_utc": "2026-03-18T03:17:26.783Z"},
]

"""Template: a source whose videos are HLS (.m3u8) playlists.

Return the playlist URL with format "hls". The server downloads it to mp4 in library mode
(no re-encoding), and for a master playlist picks the quality the client asked for
(POST /v1/tasks/{id}/library {"stream_id": ..., "quality": "720p"}; default: the best one).

You can return the master playlist as ONE stream (quality None), or one stream per quality.
Live streams and DRM-protected streams (Widevine etc.) are not supported.
This file is a template: it has no real site in it. Replace the lookup with your own source.
"""


def resolve(ctx):
    # ctx: title, year, imdb_id, season, episode, ... (see the Test panel for the full context)
    # 1) find the page/API for ctx["title"] on your source
    # 2) extract the .m3u8 URL
    playlist_url = None  # e.g. "https://cdn.example.com/films/abc/master.m3u8"
    if not playlist_url:
        return {"streams": []}
    return {
        "streams": [{
            "url": playlist_url,
            "format": "hls",
            "quality": None,
            "headers": {},   # e.g. {"Referer": "https://example.com/"} if the CDN requires it
        }],
        "subtitles": [],     # {"lang": "en", "url": "https://.../en.vtt", "format": "vtt"}
    }

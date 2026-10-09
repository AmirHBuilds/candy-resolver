"""HLS (m3u8) helpers: playlist parsing, variant choice, and the ffmpeg command. Standard library only."""
import re
from urllib.parse import urljoin

_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def parse_attrs(s: str) -> dict:
    return {k: v.strip().strip('"') for k, v in _ATTR.findall(s)}


def is_master(text: str) -> bool:
    return "#EXT-X-STREAM-INF" in text


def parse_master(text: str, base: str):
    """Returns (variants, audio_renditions). Variant: url, bandwidth, height, audio(group id)."""
    variants, audio = [], []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for idx, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF:"):
            a = parse_attrs(line.split(":", 1)[1])
            j = idx + 1
            while j < len(lines) and lines[j].startswith("#"):
                j += 1
            if j >= len(lines):
                continue
            res = a.get("RESOLUTION", "")
            height = int(res.split("x")[1]) if "x" in res and res.split("x")[1].isdigit() else None
            bw = a.get("BANDWIDTH", "")
            variants.append({"url": urljoin(base, lines[j]), "bandwidth": int(bw) if bw.isdigit() else 0,
                             "height": height, "audio": a.get("AUDIO")})
        elif line.startswith("#EXT-X-MEDIA:"):
            a = parse_attrs(line.split(":", 1)[1])
            if a.get("TYPE") == "AUDIO":
                audio.append({"group": a.get("GROUP-ID"), "default": a.get("DEFAULT") == "YES",
                              "url": urljoin(base, a["URI"]) if a.get("URI") else None})
    return variants, audio


def parse_quality(q) -> int | None:
    m = re.search(r"(\d{3,4})", str(q or ""))
    return int(m.group(1)) if m else None


def pick_variant(variants: list, quality=None):
    """Highest quality that fits `quality` (e.g. '720p'); the best one if none requested."""
    if not variants:
        return None
    key = lambda v: (v["height"] or 0, v["bandwidth"])
    want = parse_quality(quality)
    if want:
        fit = [v for v in variants if v["height"] and v["height"] <= want]
        if fit:
            return max(fit, key=key)
        with_h = [v for v in variants if v["height"]]
        if with_h:
            return min(with_h, key=key)  # everything is bigger than asked: take the smallest
    return max(variants, key=key)


def pick_audio(audio: list, group):
    """Separate audio playlist for the chosen variant, if its audio is not muxed into the video."""
    cands = [a for a in audio if a["group"] == group and a["url"]]
    if not cands:
        return None
    return next((a for a in cands if a["default"]), cands[0])


def media_info(text: str) -> dict:
    return {
        "duration": sum(float(x) for x in re.findall(r"#EXTINF:([\d.]+)", text)),
        "ended": "#EXT-X-ENDLIST" in text,
        "vod": "#EXT-X-PLAYLIST-TYPE:VOD" in text,
    }


def build_ffmpeg_cmd(ffmpeg: str, video_url: str, audio_url: str | None, out_path, headers: dict,
                     user_agent: str) -> list[str]:
    """Remux an HLS stream to mp4 without re-encoding.
    The protocol whitelist deliberately has NO `file`, so a hostile playlist cannot read local files."""
    hdr = "".join(f"{k}: {v}\r\n" for k, v in headers.items())

    def inp(u: str) -> list[str]:
        a = ["-protocol_whitelist", "http,https,tcp,tls,crypto", "-allowed_extensions", "ALL",
             "-user_agent", user_agent]
        if hdr:
            a += ["-headers", hdr]
        return a + ["-i", u]

    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1", "-y"]
    cmd += inp(video_url)
    if audio_url:
        cmd += inp(audio_url) + ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
    cmd += ["-c", "copy", "-sn", "-dn", "-movflags", "+faststart", "-f", "mp4", str(out_path)]
    return cmd


# ---------------------------------------------------------------------------------------------
# Progressive HLS (play while downloading): probing, the ffmpeg command, and playlist handling
# ---------------------------------------------------------------------------------------------
PLAYLIST_NAME = "index.m3u8"
SEG_NAME = re.compile(r"seg_\d{5}\.ts")
OK_PIX_FMTS = {"yuv420p", "yuvj420p"}      # what browsers can decode (8-bit 4:2:0)


def valid_hls_filename(name: str) -> bool:
    """Only these exact names are ever served: nothing else can be requested, so there is no traversal."""
    return name == PLAYLIST_NAME or bool(SEG_NAME.fullmatch(name))


def input_opts(headers: dict, user_agent: str, hls_source: bool) -> list[str]:
    """Options placed before an input. Plain http gets reconnect + stall timeout; no `file:` protocol ever."""
    hdr = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    o = ["-protocol_whitelist", "http,https,tcp,tls,crypto", "-user_agent", user_agent]
    if hdr:
        o += ["-headers", hdr]
    if hls_source:
        o += ["-allowed_extensions", "ALL"]
    else:
        o += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5", "-rw_timeout", "30000000"]
    return o


def parse_probe(data: dict) -> dict:
    """Pick what we need out of `ffprobe -show_streams -show_format -of json`."""
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)   # skip cover art
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    def num(x):
        try:
            v = float(x)
            return v if v > 0 else None
        except (TypeError, ValueError):
            return None

    size = fmt.get("size")
    return {
        "video_index": video["index"] if video else None,
        "video_codec": video.get("codec_name") if video else None,
        "pix_fmt": video.get("pix_fmt") if video else None,
        "height": video.get("height") if video else None,
        "audio_index": audio["index"] if audio else None,
        "audio_codec": audio.get("codec_name") if audio else None,
        "duration": num(fmt.get("duration")) or (num(video.get("duration")) if video else None),
        "size": int(size) if str(size or "").isdigit() else None,
    }


def validate_probe(info: dict) -> None:
    if info["video_index"] is None:
        raise ValueError("no video stream in the source")
    if info["video_codec"] != "h264":
        raise ValueError(f"unsupported video codec: {info['video_codec']}")
    if info["pix_fmt"] and info["pix_fmt"] not in OK_PIX_FMTS:
        raise ValueError(f"unsupported video format: {info['pix_fmt']}")


def audio_mode(codec: str | None) -> str:
    """copy: aac/mp3 go straight in. aac: anything else is converted (cheap). none: no audio at all."""
    if not codec:
        return "none"
    return "copy" if codec in ("aac", "mp3") else "aac"


def build_progressive_cmd(ffmpeg: str, video_url: str, audio_url: str | None, out_dir, headers: dict,
                          user_agent: str, vinfo: dict, ainfo: dict | None, hls_source: bool,
                          segment_seconds: int = 4, audio_bitrate: str = "128k") -> list[str]:
    """ffmpeg that copies the video (never re-encodes it) into a growing HLS playlist + .ts segments.
    No -re: it downloads as fast as the source allows. Subtitles and data streams are dropped."""
    out_dir = str(out_dir)
    a_src = ainfo if audio_url else vinfo           # where the audio stream lives
    a_in = 1 if audio_url else 0
    mode = audio_mode(a_src["audio_codec"] if a_src else None)

    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1", "-y"]
    cmd += input_opts(headers, user_agent, hls_source) + ["-i", video_url]
    if audio_url:
        cmd += input_opts(headers, user_agent, hls_source) + ["-i", audio_url]
    cmd += ["-map", f"0:{vinfo['video_index']}", "-c:v", "copy"]
    if mode == "none":
        cmd += ["-an"]
    else:
        cmd += ["-map", f"{a_in}:{a_src['audio_index']}"]
        cmd += ["-c:a", "copy"] if mode == "copy" else ["-c:a", "aac", "-b:a", audio_bitrate, "-ac", "2"]
    cmd += ["-sn", "-dn", "-f", "hls", "-hls_time", str(segment_seconds), "-hls_list_size", "0",
            "-hls_playlist_type", "event", "-hls_flags", "independent_segments+temp_file",
            "-hls_segment_type", "mpegts", "-hls_segment_filename", f"{out_dir}/seg_%05d.ts",
            f"{out_dir}/{PLAYLIST_NAME}"]
    return cmd


def count_listed_segments(text: str) -> int:
    return len(re.findall(r"^seg_\d{5}\.ts\s*$", text, re.M))


def has_endlist(text: str) -> bool:
    return "#EXT-X-ENDLIST" in text


def rewrite_playlist(text: str, query: str, exists) -> str:
    """Append ?query (exp + sig) to every segment URI, because relative URLs do not inherit the playlist's
    query string. Stops at the first segment that is not a finished file on disk (and then also drops ENDLIST),
    so a half-written playlist can never point at something missing."""
    out, pending = [], None
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#EXTINF"):
            pending = line
        elif s.startswith("#"):
            out.append(line)
        else:
            if SEG_NAME.fullmatch(s) and exists(s):
                if pending:
                    out.append(pending)
                out.append(f"{s}?{query}")
                pending = None
            else:
                break            # not ready (or unexpected): stop here
    else:
        return "\n".join(out) + "\n"
    out = [ln for ln in out if not ln.strip().startswith("#EXT-X-ENDLIST")]
    return "\n".join(out) + "\n"

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

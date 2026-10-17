"""Subtitle handling, pure Python and cheap: language names, text decoding, format detection, SRT -> WebVTT."""
import re

LANGS = {
    "en": "English", "fa": "Persian", "ar": "Arabic", "es": "Spanish", "fr": "French", "de": "German", "it": "Italian",
    "pt": "Portuguese", "ru": "Russian", "tr": "Turkish", "hi": "Hindi", "ja": "Japanese", "ko": "Korean", "zh": "Chinese",
    "nl": "Dutch", "pl": "Polish", "sv": "Swedish", "no": "Norwegian", "da": "Danish", "fi": "Finnish", "el": "Greek",
    "he": "Hebrew", "hu": "Hungarian", "cs": "Czech", "ro": "Romanian", "bg": "Bulgarian", "uk": "Ukrainian",
    "vi": "Vietnamese", "th": "Thai", "id": "Indonesian", "ms": "Malay", "bn": "Bengali", "ur": "Urdu", "sr": "Serbian",
    "hr": "Croatian", "sk": "Slovak", "sl": "Slovenian", "ca": "Catalan", "lt": "Lithuanian", "lv": "Latvian",
    "et": "Estonian", "is": "Icelandic", "ku": "Kurdish", "ps": "Pashto", "az": "Azerbaijani", "hy": "Armenian",
    "ka": "Georgian", "sq": "Albanian", "mk": "Macedonian", "bs": "Bosnian", "ta": "Tamil", "te": "Telugu",
    "ml": "Malayalam", "mr": "Marathi", "ne": "Nepali", "si": "Sinhala", "tl": "Filipino", "sw": "Swahili",
    "af": "Afrikaans", "kk": "Kazakh", "uz": "Uzbek", "mn": "Mongolian", "my": "Burmese", "km": "Khmer", "lo": "Lao",
    "eu": "Basque", "gl": "Galician", "cy": "Welsh", "ga": "Irish", "mt": "Maltese", "be": "Belarusian", "la": "Latin",
}
ISO3 = {  # ISO 639-2 (B and T) -> 639-1
    "eng": "en", "fas": "fa", "per": "fa", "ara": "ar", "spa": "es", "fra": "fr", "fre": "fr", "deu": "de", "ger": "de",
    "ita": "it", "por": "pt", "rus": "ru", "tur": "tr", "hin": "hi", "jpn": "ja", "kor": "ko", "zho": "zh", "chi": "zh",
    "nld": "nl", "dut": "nl", "pol": "pl", "swe": "sv", "nor": "no", "nob": "no", "dan": "da", "fin": "fi", "ell": "el",
    "gre": "el", "heb": "he", "hun": "hu", "ces": "cs", "cze": "cs", "ron": "ro", "rum": "ro", "bul": "bg", "ukr": "uk",
    "vie": "vi", "tha": "th", "ind": "id", "msa": "ms", "may": "ms", "ben": "bn", "urd": "ur", "srp": "sr", "hrv": "hr",
    "slk": "sk", "slo": "sk", "slv": "sl", "cat": "ca", "lit": "lt", "lav": "lv", "est": "et", "isl": "is", "ice": "is",
    "kur": "ku", "pus": "ps", "aze": "az", "hye": "hy", "arm": "hy", "kat": "ka", "geo": "ka", "sqi": "sq", "alb": "sq",
    "mkd": "mk", "mac": "mk", "bos": "bs", "tam": "ta", "tel": "te", "mal": "ml", "mar": "mr", "nep": "ne", "sin": "si",
    "fil": "tl", "tgl": "tl", "swa": "sw", "afr": "af", "kaz": "kk", "uzb": "uz", "mon": "mn", "mya": "my", "bur": "my",
    "khm": "km", "lao": "lo", "eus": "eu", "baq": "eu", "glg": "gl", "cym": "cy", "wel": "cy", "gle": "ga", "mlt": "mt",
    "bel": "be", "lat": "la",
}
NAMES = {n.lower(): c for c, n in LANGS.items()}
NAMES.update({"farsi": "fa", "castilian": "es", "flemish": "nl", "mandarin": "zh", "cantonese": "zh", "brazilian": "pt"})

# legacy single-byte code pages, tried (after utf-8) according to the subtitle's language
FALLBACK_CP = {
    "fa": "cp1256", "ar": "cp1256", "ur": "cp1256", "ps": "cp1256", "ku": "cp1256", "he": "cp1255",
    "ru": "cp1251", "uk": "cp1251", "bg": "cp1251", "be": "cp1251", "sr": "cp1251", "mk": "cp1251", "el": "cp1253",
    "tr": "cp1254", "az": "cp1254", "pl": "cp1250", "cs": "cp1250", "sk": "cp1250", "hu": "cp1250", "ro": "cp1250",
    "hr": "cp1250", "sl": "cp1250", "sq": "cp1250", "bs": "cp1250", "lt": "cp1257", "lv": "cp1257", "et": "cp1257",
    "vi": "cp1258", "th": "cp874",
}


class SubtitleRejected(Exception):
    """The file is not a usable subtitle. The reason is short and only used for logs/tests."""


def normalize_lang(value) -> str:
    """'eng', 'en-US', 'English', 'fas', 'Persian', 'pt_BR' ... -> 'en', 'en', 'en', 'fa', 'fa', 'pt'. Unknown -> 'und'."""
    if not isinstance(value, str) or not value.strip():
        return "und"
    s = value.strip().lower()
    primary = re.split(r"[-_\s.]", s, maxsplit=1)[0]
    if len(primary) == 2 and primary.isalpha():
        return primary
    if len(primary) == 3 and primary in ISO3:
        return ISO3[primary]
    for w in re.findall(r"[a-z]+", s):
        if w in NAMES:
            return NAMES[w]
    return "und"


def lang_label(code: str) -> str:
    return LANGS.get(code) or ("Unknown" if code == "und" else code.upper())


# --------------------------------------------------------------------------------------------- decoding
def decode_text(raw: bytes, lang: str = "und") -> tuple[str, str]:
    """Bytes -> (text, encoding used). utf-8, then utf-16, then a legacy code page chosen by language."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace"), "utf-8"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            raise SubtitleRejected("broken utf-16 file")
    sample = raw[:4096]
    if b"\x00" in sample:                                   # utf-16 without BOM, or binary
        odd_zero = sample[1::2].count(0) / max(1, len(sample[1::2]))
        even_zero = sample[0::2].count(0) / max(1, len(sample[0::2]))
        for enc, ratio in (("utf-16-le", odd_zero), ("utf-16-be", even_zero)):
            if ratio > 0.4:
                try:
                    return raw.decode(enc), enc
                except UnicodeDecodeError:
                    pass
        raise SubtitleRejected("binary file")
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    for enc in (FALLBACK_CP.get(lang), "cp1252"):
        if enc:
            try:
                return raw.decode(enc), enc
            except UnicodeDecodeError:
                continue
    return raw.decode("latin-1"), "latin-1"


# --------------------------------------------------------------------------------------------- detection
_TIMING = r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
SRT_TIMING = re.compile(_TIMING)
SRT_TIMING_LINE = re.compile(r"^\s*" + _TIMING)
VTT_HEADER = re.compile(r"WEBVTT(?:[ \t\r\n]|$)")


def sniff(raw: bytes, lang: str = "und") -> tuple[str, str, str]:
    """Look at the CONTENT (not the file name or declared format): -> (kind, text, encoding).
    kind is 'vtt', 'srt' or 'ass'. Anything else (empty, binary, web page, unknown) raises SubtitleRejected."""
    if not raw or not raw.strip():
        raise SubtitleRejected("empty file")
    text, enc = decode_text(raw, lang)
    body = text.lstrip("\ufeff \t\r\n")
    if not body:
        raise SubtitleRejected("empty file")
    sample = body[:20000]
    ctrl = sum(1 for ch in sample if ord(ch) < 32 and ch not in "\t\r\n")
    if ctrl > max(3, len(sample) // 100):
        raise SubtitleRejected("binary file")
    head = body[:2048].lower()
    if VTT_HEADER.match(body):
        return "vtt", text, enc
    if "[script info]" in head or "[v4+ styles]" in head or "[v4 styles]" in head or \
            ("[events]" in sample.lower() and "dialogue:" in sample.lower()):
        return "ass", text, enc
    if head.startswith(("<!doctype", "<html", "<?xml", "<body", "<head", "{")) or "<html" in head or "<body" in head:
        raise SubtitleRejected("not a subtitle (web page / error page / data)")
    if SRT_TIMING.search(body):
        return "srt", text, enc
    raise SubtitleRejected("unrecognised subtitle format")


# --------------------------------------------------------------------------------------------- conversion
_ASS_TAGS = re.compile(r"\{\\[^}]*\}")
_OTHER_TAGS = re.compile(r"</?(?!(?:i|b|u)\b)[a-zA-Z][^>]*>")        # <font>, <span>, <br> ... (i, b, u are valid in WebVTT)
_BAD_LT = re.compile(r"<(?!/?[ibu]>)")
_BAD_AMP = re.compile(r"&(?!(?:amp|lt|gt|nbsp|lrm|rlm|#\d+|#x[0-9a-fA-F]+);)")


def _clean_line(line: str) -> str:
    line = _ASS_TAGS.sub("", line)
    line = _OTHER_TAGS.sub("", line)
    line = _BAD_LT.sub("&lt;", line)
    line = _BAD_AMP.sub("&amp;", line)
    return line.replace("-->", "->").rstrip()


def _ts(h, m, s, ms) -> tuple[str, int]:
    h, m, s, ms = int(h), int(m), int(s), int(ms.ljust(3, "0")[:3])
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}", ((h * 60 + m) * 60 + s) * 1000 + ms


def srt_to_vtt(text: str) -> tuple[str, int]:
    """SRT -> (WebVTT text, number of cues): header added, comma -> dot, BOM stripped, line endings fixed,
    cue numbers and position junk dropped, font/ASS tags removed, invalid cues (end <= start, no text) skipped."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").split("\n")
    cues, cur = [], None

    def flush():
        nonlocal cur
        if cur and cur["text"] and cur["end_ms"] > cur["start_ms"]:
            cues.append(f"{cur['start']} --> {cur['end']}\n" + "\n".join(cur["text"]))
        cur = None

    for line in lines:
        m = SRT_TIMING_LINE.match(line)
        if m:
            if cur and cur["text"] and cur["text"][-1].strip().isdigit():
                cur["text"].pop()                       # the next cue's number glued on (no blank line between cues)
            flush()
            g = m.groups()
            start, s_ms = _ts(*g[0:4])
            end, e_ms = _ts(*g[4:8])
            cur = {"start": start, "end": end, "start_ms": s_ms, "end_ms": e_ms, "text": []}
        elif cur is not None:
            if not line.strip():
                flush()
            else:
                cleaned = _clean_line(line)
                if cleaned.strip():
                    cur["text"].append(cleaned)
    flush()
    if not cues:
        raise SubtitleRejected("no usable cues")
    return "WEBVTT\n\n" + "\n\n".join(cues) + "\n", len(cues)


def normalize_vtt(text: str) -> tuple[str, int]:
    """Keep a VTT as it is, only fixing BOM and line endings. Needs the header and at least one cue."""
    t = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff \t\n")
    if not VTT_HEADER.match(t):
        raise SubtitleRejected("not a WebVTT file")
    cues = t.count("-->")
    if cues == 0:
        raise SubtitleRejected("no cues")
    return t.rstrip("\n") + "\n", cues


def to_vtt(kind: str, text: str) -> tuple[str, int]:
    if kind == "vtt":
        return normalize_vtt(text)
    if kind == "srt":
        return srt_to_vtt(text)
    raise SubtitleRejected(f"unsupported subtitle format: {kind}")

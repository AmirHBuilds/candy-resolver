"""The Telegram daily report: turns collected numbers into one message, and decides when it is due. Pure logic, no database."""
import html
from datetime import datetime, timedelta, timezone

MAX_SOURCES = 10


def _e(x) -> str:
    return html.escape(str(x), quote=False)


def fmt_bytes(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000


def fmt_ms(ms) -> str:
    return "–" if ms is None else (f"{ms / 1000:.1f} s" if ms >= 1000 else f"{int(ms)} ms")


def fmt_uptime(s) -> str:
    s = int(s or 0)
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    return f"{d}d {h}h" if d else f"{h}h {r // 60}m"


def source_health(s: dict) -> str:
    """🔴 failing a lot, 🟠 mostly finds nothing, 🟢 fine."""
    runs = s["runs"]
    if runs >= 3 and s["error"] / runs >= 0.5:
        return "🔴"
    if runs >= 5 and s["ok"] / runs < 0.25:
        return "🟠"
    return "🟢"


def format_report(d: dict, server: str, now: datetime) -> str:
    t, dl, lib, sy = d["tasks"], d["downloads"], d["library"], d.get("system") or {}
    out = [f"📊 <b>Daily report</b> · {_e(server)}", f"<i>last {d.get('period_h', 24)} h · {now:%Y-%m-%d %H:%M} UTC</i>", ""]

    out.append(f"🧩 <b>Tasks:</b> {t['total']}" + (f" ({t['done']} done · {t['failed']} failed)" if t["total"] else ""))
    srcs = sorted(d.get("sources") or [], key=lambda s: ({"🔴": 0, "🟠": 1, "🟢": 2}[source_health(s)], -s["runs"]))
    if srcs:
        out.append("<b>Sources</b> (runs · found streams · avg time)")
        for s in srcs[:MAX_SOURCES]:
            found = round(100 * s["ok"] / s["runs"]) if s["runs"] else 0
            line = f"{source_health(s)} {_e(s['name'])} — {s['runs']} · {found}% · {fmt_ms(s.get('avg_ms'))}"
            if s["error"]:
                line += f" · {s['error']} error(s)"
            out.append(line)
        if len(srcs) > MAX_SOURCES:
            out.append(f"… and {len(srcs) - MAX_SOURCES} more")
    elif t["total"] == 0:
        out.append("No tasks were requested.")

    out.append("")
    out.append(f"⬇️ <b>Downloads:</b> {dl['ready']} finished ({fmt_bytes(dl['ready_bytes'])}) · {dl['failed']} failed"
               + (f" · {dl['other']} still running / queued" if dl.get("other") else ""))
    for reason, n in (dl.get("reasons") or [])[:3]:
        out.append(f"   • {n}× {_e(reason)}")
    out.append(f"📚 <b>Library now:</b> {lib['items']} item(s), {fmt_bytes(lib['bytes'])}")

    if sy:
        free, total = sy.get("disk_free", 0), sy.get("disk_total", 0)
        pct = f" ({100 * free / total:.0f}% free)" if total else ""
        out.append(f"💾 <b>Disk:</b> {fmt_bytes(free)} free of {fmt_bytes(total)}{pct}")
        bits = [f"up {fmt_uptime(sy.get('uptime_s'))}"]
        if sy.get("load") is not None:
            bits.append(f"load {sy['load']}")
        if sy.get("mem_percent") is not None:
            bits.append(f"RAM {sy['mem_percent']:.0f}%")
        if d.get("version"):
            bits.append(f"v{_e(d['version'])}")
        out.append("🖥 <b>Server:</b> " + " · ".join(bits))
    ac = d.get("autoclean")
    if ac:
        ago = max(0, int((now.timestamp() - ac["at"]) / 3600))
        out.append(f"🧹 <b>Last auto-clean:</b> freed {fmt_bytes(ac['freed_bytes'])}, {ago} h ago" + (f" · {ac['errors']} error(s)" if ac.get("errors") else ""))

    errs = d.get("errors_logged", 0)
    out.append(("⚠️" if errs else "✅") + f" <b>Server errors logged:</b> {errs}")
    return "\n".join(out)[:4000]


def report_due(now_utc: datetime, time_str: str, utc_offset: float, last_sent: str | None) -> tuple[bool, str]:
    """Is the daily report due now? Returns (due, today's date in the configured time zone). It is due once per local day,
    from the chosen time on (so a server that was down at that time still sends it when it comes back)."""
    local = now_utc + timedelta(hours=utc_offset)
    try:
        h, m = (int(x) for x in time_str.split(":"))
    except (ValueError, AttributeError):
        h, m = 8, 0
    today = local.date().isoformat()
    return (last_sent != today and (local.hour, local.minute) >= (h, m)), today

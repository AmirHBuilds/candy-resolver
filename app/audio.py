"""Audio-only extraction from a finished library item: ffmpeg writes to a pipe and we stream it to the client.
No web framework and no app imports in here, so it can be tested with a real ffmpeg (tests/smoke_audio.py)."""
import asyncio
import json
import os
import signal

# format -> (content type, file extension, ffmpeg muxer, encoder arguments)
FORMATS = {
    "aac": ("audio/aac", "aac", "adts", ["-c:a", "aac", "-b:a", "128k"]),
    "mp3": ("audio/mpeg", "mp3", "mp3", ["-c:a", "libmp3lame", "-b:a", "128k"]),
    "opus": ("audio/ogg", "opus", "ogg", ["-c:a", "libopus", "-b:a", "64k"]),
}
CHUNK = 64 * 1024


class AudioError(Exception):
    def __init__(self, message: str, status: int = 500):
        super().__init__(message)
        self.status = status


def _kill(proc) -> None:
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError):
        pass


async def probe_audio(ffprobe: str, path, timeout: float = 30) -> tuple[list[str], float | None]:
    """(codec name of every audio track - empty list = no audio at all, file duration in seconds if known)."""
    cmd = [ffprobe, "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_name:format=duration", "-of", "json", str(path)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
    except FileNotFoundError:
        raise AudioError("ffprobe is not installed on the server")
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        _kill(proc)
        raise AudioError("could not read the media file (timed out)")
    if proc.returncode != 0:
        raise AudioError("could not read the media file")
    try:
        data = json.loads(out or b"{}")
    except ValueError:
        raise AudioError("could not read the media file")
    try:
        duration = float((data.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        duration = None
    return [str(s.get("codec_name") or "") for s in data.get("streams", [])], duration


def build_cmd(ffmpeg: str, path, fmt: str, track: int, source_codec: str,
              start: float | None = None, duration: float | None = None) -> list[str]:
    """ffmpeg command that writes ONLY audio to stdout. AAC sources are copied as they are (no re-encoding)."""
    _, _, mux, enc = FORMATS[fmt]
    if fmt == "aac" and source_codec == "aac":
        enc = ["-c:a", "copy"]
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(path)]
    if duration:
        cmd += ["-t", f"{duration:.3f}"]
    return cmd + ["-map", f"0:a:{track}", "-vn", "-sn", "-dn", *enc, "-f", mux, "pipe:1"]


class AudioStream:
    """start() runs ffmpeg and waits for the first bytes, so a failure becomes a clean error response instead of a
    broken 200. chunks() then streams the rest; the process is always killed when the client goes away."""

    def __init__(self, cmd: list[str]):
        self.cmd = cmd
        self.proc = None
        self._first = b""
        self._err_tail = b""
        self._drain_task = None

    async def _drain(self) -> None:
        while True:
            data = await self.proc.stderr.read(4096)
            if not data:
                return
            self._err_tail = (self._err_tail + data)[-1500:]

    def _error_text(self) -> str:
        return self._err_tail.decode("utf-8", "replace").strip().splitlines()[-1][:300] if self._err_tail.strip() else ""

    async def start(self) -> None:
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self.cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        except FileNotFoundError:
            raise AudioError("ffmpeg is not installed on the server")
        self._drain_task = asyncio.ensure_future(self._drain())
        try:
            self._first = await self.proc.stdout.read(CHUNK)
            if not self._first:
                await self.proc.wait()
                await asyncio.wait_for(asyncio.shield(self._drain_task), 2)
                if self.proc.returncode == 0:
                    raise AudioError("there is no audio in that range", 422)
                raise AudioError("could not extract audio: " + (self._error_text() or f"ffmpeg exited with code {self.proc.returncode}"))
        except BaseException:
            self.close()
            raise

    async def chunks(self):
        try:
            yield self._first
            while True:
                data = await self.proc.stdout.read(CHUNK)
                if not data:
                    break
                yield data
            await self.proc.wait()
            if self.proc.returncode != 0:        # raising aborts the response: the client must not mistake a cut-off file for a whole one
                raise AudioError("audio extraction failed half way: " + self._error_text())
        finally:
            self.close()

    def close(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            _kill(self.proc)
        if self._drain_task is not None and not self._drain_task.done():
            self._drain_task.cancel()

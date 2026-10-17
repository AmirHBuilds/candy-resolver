"""Link to the host agent (scripts/host_cleanup.sh --install-agent): lets the admin panel ask for a clean of the VPS itself.

The container only ever (a) writes a tiny request file {id, aggressive, dry_run} and (b) reads status.json that the agent writes.
The agent runs on the host as root but reads nothing from the request except those two true/false flags, so this gives the app
no access to the host. No database and no web framework in here."""
import json
import os
import secrets
import tempfile
import time
from pathlib import Path

ALIVE_S = 150          # the agent writes a heartbeat every minute; older than this = not running


class HostClean:
    def __init__(self, directory=None):
        self._dir_arg = directory

    @property
    def dir(self) -> Path:
        if self._dir_arg:
            return Path(self._dir_arg)
        from .config import settings
        return Path(settings.hostclean_dir)

    def status(self, now: float | None = None) -> dict:
        now = now or time.time()
        d = self.dir
        try:
            mounted = d.is_dir()
        except OSError:
            mounted = False
        base = {"configured": mounted, "agent_alive": False, "agent_seen_at": None, "pending": False, "running": False, "last": None, "host": None}
        if not mounted:
            return base
        try:
            st = json.loads((d / "status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            st = {}
        agent = st.get("agent") if isinstance(st.get("agent"), dict) else {}
        seen = agent.get("alive_at") if isinstance(agent.get("alive_at"), (int, float)) else None
        base.update(agent_alive=bool(seen and now - seen <= ALIVE_S), agent_seen_at=seen, host=agent.get("host"),
                    pending=(d / "request.json").exists(), running=bool(st.get("running")),
                    last=st.get("last") if isinstance(st.get("last"), dict) else None)
        return base

    def request(self, aggressive: bool = False, dry_run: bool = False, now: float | None = None) -> dict:
        """Ask the agent to clean the host. Never blocks: the agent picks the file up within a minute."""
        st = self.status(now)
        if not st["configured"]:
            return {"ok": False, "reason": "not_mounted",
                    "error": "The host folder is not mounted into the app (docker-compose.yml must have ./hostclean:/hostclean). Update the compose file and restart."}
        if not st["agent_alive"]:
            return {"ok": False, "reason": "no_agent",
                    "error": "The host agent is not running. On the VPS run: sudo ./scripts/host_cleanup.sh --install-agent"}
        if st["pending"]:
            return {"ok": True, "already": True, "id": None}
        rid = secrets.token_hex(6)
        body = json.dumps({"id": rid, "aggressive": bool(aggressive), "dry_run": bool(dry_run), "at": int(now or time.time())})
        fd, tmp = tempfile.mkstemp(dir=str(self.dir), prefix=".req-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(body)
            os.chmod(tmp, 0o644)
            os.replace(tmp, self.dir / "request.json")
        except OSError as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return {"ok": False, "reason": "write_failed", "error": f"could not write the request: {e}"}
        return {"ok": True, "id": rid}


hostclean = HostClean()

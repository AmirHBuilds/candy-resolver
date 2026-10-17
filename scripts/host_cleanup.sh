#!/usr/bin/env bash
# candyresolver HOST cleanup: frees disk space on the VPS itself (outside the app container).
#
#   sudo ./scripts/host_cleanup.sh --dry-run      show what would be cleaned, change nothing
#   sudo ./scripts/host_cleanup.sh                clean now
#   sudo ./scripts/host_cleanup.sh --aggressive   also remove ALL unused Docker images and build cache older than 7 days
#   sudo ./scripts/host_cleanup.sh --install      run it automatically every Sunday 04:17 (--daily for every day); --uninstall removes it
#
#   sudo ./scripts/host_cleanup.sh --install-agent   let the admin panel's "Clean disk" also clean this host (checks a request file every minute; --uninstall-agent removes it)
#
# Options: --app-clean   also ask the running app to run its own disk clean (needs CANDY_ADMIN_TOKEN, CANDY_URL=http://localhost:8000)
#          --skip a,b    skip sections: docker journal apt snap logs tmp cache crash trim
#          --quiet       only print problems and the one-line summary (used by cron)
#
# SAFE BY DESIGN: it never runs `docker volume prune`, never touches Docker volumes (your database, secrets and library live
# there), never stops or removes a running container or an image that a container uses, and never deletes your project folder.
set -u
export LC_ALL=C DEBIAN_FRONTEND=noninteractive

DRY=0 AGGR=0 QUIET=0 APP=0 ACTION=run SCHEDULE=weekly SKIP=",${HC_SKIP:-},"
DOCKER_AGE="${HC_DOCKER_AGE:-168h}"                 # only Docker things unused for this long
TMP_DIRS="${HC_TMP_DIRS:-/tmp /var/tmp}"; TMP_DAYS="${HC_TMP_DAYS:-7}"
LOG_DIR="${HC_LOG_DIR:-/var/log}"; LOG_DAYS="${HC_LOG_DAYS:-14}"; BIG_LOG_MB="${HC_BIG_LOG_MB:-500}"
DOCKER_LOG_MB="${HC_DOCKER_LOG_MB:-200}"; DOCKER_LOG_GLOB="${HC_DOCKER_LOG_GLOB:-/var/lib/docker/containers/*/*-json.log}"
JOURNAL_SIZE="${HC_JOURNAL_SIZE:-200M}"; JOURNAL_DAYS="${HC_JOURNAL_DAYS:-14}"
ROOT_FS="${HC_ROOT_FS:-/}"
CRON_FILE=/etc/cron.d/candyresolver-host-cleanup; LOGROTATE_FILE=/etc/logrotate.d/candyresolver-host-cleanup

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; }
say()  { [ "$QUIET" = 1 ] || echo "$@"; }
warn() { echo "WARNING: $*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }
want() { case "$SKIP" in *",$1,"*) return 1;; esac; return 0; }
used_kb() { df -Pk "$ROOT_FS" 2>/dev/null | awk 'NR==2{print $3}'; }
human() { awk -v k="${1:-0}" 'BEGIN{ s=k*1024; split("B KB MB GB TB",u," "); i=1; while (s>=1024 && i<5) {s/=1024; i++}; printf "%.1f %s", s, u[i] }'; }

while [ $# -gt 0 ]; do
  case "$1" in
    -n|--dry-run) DRY=1;;
    --aggressive) AGGR=1;;
    -q|--quiet) QUIET=1;;
    --app-clean) APP=1;;
    --install) ACTION=install;;
    --uninstall) ACTION=uninstall;;
    --daily) SCHEDULE=daily;;
    --weekly) SCHEDULE=weekly;;
    --skip) shift; SKIP=",${1:-},";;
    --install-agent) ACTION=install-agent;;
    --uninstall-agent) ACTION=uninstall-agent;;
    --agent-tick) ACTION=agent-tick;;
    --hostclean-dir) shift; HC_AGENT_DIR="${1:-}";;
    -h|--help) usage; exit 0;;
    *) echo "unknown option: $1"; usage; exit 2;;
  esac
  shift
done

need_root() { [ "$(id -u)" -eq 0 ] || [ "${HC_ALLOW_NONROOT:-0}" = 1 ] || { echo "Please run with sudo (or use --dry-run)."; exit 1; }; }

# ---------------------------------------------------------------- install / uninstall
if [ "$ACTION" = uninstall ]; then
  need_root; rm -f "$CRON_FILE" "$LOGROTATE_FILE"; echo "Automatic host cleanup removed."; exit 0
fi
if [ "$ACTION" = install ]; then
  need_root
  me="$(readlink -f "$0")"; flags="--quiet"; [ "$AGGR" = 1 ] && flags="$flags --aggressive"; [ "$APP" = 1 ] && flags="$flags --app-clean"
  [ "$SKIP" != "," ] && flags="$flags --skip ${SKIP#,}" && flags="${flags%,}"
  when="17 4 * * 0"; [ "$SCHEDULE" = daily ] && when="17 4 * * *"
  cat > "$CRON_FILE" <<CRON
# candyresolver host cleanup (installed by host_cleanup.sh --install)
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin
$when root $me $flags >> /var/log/candyresolver-host-cleanup.log 2>&1
CRON
  chmod 644 "$CRON_FILE"
  printf '/var/log/candyresolver-host-cleanup.log {\n    weekly\n    rotate 4\n    compress\n    missingok\n    notifempty\n}\n' > "$LOGROTATE_FILE"
  echo "Installed: $SCHEDULE at 04:17 -> $CRON_FILE (log: /var/log/candyresolver-host-cleanup.log). Remove with --uninstall."
  exit 0
fi

# ---------------------------------------------------------------- host agent: the admin panel asks, this does it
# The app (inside Docker) can only drop a tiny request file into AGENT_DIR (bind-mounted as /hostclean). Every minute this agent looks for it,
# accepts ONLY the flags aggressive / dry_run (true or false, nothing else is ever read from the file), runs this very script, and
# writes the outcome to status.json / last.json for the panel to show. The container never gets any access to the host.
ME="$(readlink -f "$0")"
AGENT_DIR="${HC_AGENT_DIR:-$(cd "$(dirname "$ME")/.." && pwd)/hostclean}"
AGENT_CRON=/etc/cron.d/candyresolver-host-agent; AGENT_LOGROTATE=/etc/logrotate.d/candyresolver-host-agent

if [ "$ACTION" = uninstall-agent ]; then need_root; rm -f "$AGENT_CRON" "$AGENT_LOGROTATE"; echo "Host agent removed."; exit 0; fi
if [ "$ACTION" = install-agent ]; then
  need_root; mkdir -p "$AGENT_DIR" && chmod 755 "$AGENT_DIR"
  cat > "$AGENT_CRON" <<CRON
# candyresolver host agent: lets the admin panel clean this host too (installed by host_cleanup.sh --install-agent)
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin
* * * * * root HC_AGENT_DIR=$AGENT_DIR $ME --agent-tick >> /var/log/candyresolver-host-agent.log 2>&1
CRON
  chmod 644 "$AGENT_CRON"
  printf '/var/log/candyresolver-host-agent.log {\n    weekly\n    rotate 2\n    compress\n    missingok\n    notifempty\n}\n' > "$AGENT_LOGROTATE"
  HC_AGENT_DIR="$AGENT_DIR" "$ME" --agent-tick
  echo "Host agent installed: it checks $AGENT_DIR every minute. In the admin panel, tick \"Also clean the host\" in Files on disk -> Clean disk."
  echo "(docker-compose.yml mounts that folder into the app as /hostclean.) Remove with --uninstall-agent."
  exit 0
fi
if [ "$ACTION" = agent-tick ]; then
  jesc() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr '\t' ' ' | tr -d '\000-\010\013-\037'; }
  put() { printf '%s\n' "$2" > "$AGENT_DIR/.tmp.$$" && chmod 644 "$AGENT_DIR/.tmp.$$" && mv -f "$AGENT_DIR/.tmp.$$" "$AGENT_DIR/$1"; }
  write_status() {
    local last; last="$(cat "$AGENT_DIR/last.json" 2>/dev/null)"; [ -n "$last" ] || last=null
    put status.json "{\"agent\":{\"version\":1,\"alive_at\":$(date +%s),\"host\":\"$(jesc "$(hostname)")\"},\"running\":$1,\"last\":$last}"
  }
  mkdir -p "$AGENT_DIR" 2>/dev/null || { echo "cannot create $AGENT_DIR"; exit 1; }
  exec 8>"$AGENT_DIR/.agent.lock"
  if have flock && ! flock -n 8; then write_status true; exit 0; fi          # a cleanup is running: just show we are alive
  write_status false
  [ -f "$AGENT_DIR/request.json" ] || exit 0
  req="$(head -c 2000 "$AGENT_DIR/request.json" 2>/dev/null | tr -d '\000')"; rm -f "$AGENT_DIR/request.json"
  id="$(printf '%s' "$req" | sed -n 's/.*"id" *: *"\([0-9a-f]\{6,32\}\)".*/\1/p' | head -n 1)"; [ -n "$id" ] || id=unknown
  flags=(); a=false; d=false
  printf '%s' "$req" | grep -Eq '"aggressive" *: *true' && { flags+=(--aggressive); a=true; }
  printf '%s' "$req" | grep -Eq '"dry_run" *: *true' && { flags+=(--dry-run); d=true; }
  write_status true
  start="$(date +%s)"; before="$(used_kb)"
  out="$("$ME" "${flags[@]}" 2>&1)"; rc=$?
  after="$(used_kb)"; finish="$(date +%s)"
  freed=$(( ${before:-0} - ${after:-0} )); [ "$freed" -lt 0 ] && freed=0
  logj=""; while IFS= read -r line; do logj="$logj\"$(jesc "$line")\","; done < <(printf '%s\n' "$out" | tail -n 60)
  free_kb="$(df -Pk "$ROOT_FS" 2>/dev/null | awk 'NR==2{print $4}')"
  okv=true; [ "$rc" -ne 0 ] && okv=false
  put last.json "{\"id\":\"$id\",\"dry_run\":$d,\"aggressive\":$a,\"started_at\":$start,\"finished_at\":$finish,\"ok\":$okv,\"freed_kb\":$freed,\"before_used_kb\":${before:-0},\"after_used_kb\":${after:-0},\"free_kb\":${free_kb:-0},\"log\":[${logj%,}]}"
  write_status false
  exit 0
fi

# ---------------------------------------------------------------- helpers for cleaning
[ "$DRY" = 1 ] || need_root
if [ "$DRY" = 0 ] && have flock && [ -w /var/lock ]; then       # never two cleanups at once
  exec 9>/var/lock/candyresolver-host-cleanup.lock; flock -n 9 || { echo "Another host cleanup is already running."; exit 0; }
fi

# run <command...>: print it, run it unless --dry-run, show Docker's "reclaimed" line
run() {
  say "  \$ $*"
  [ "$DRY" = 1 ] && return 0
  local out; out="$("$@" 2>&1)"; local rc=$?
  [ $rc -ne 0 ] && warn "$* -> exit $rc: $(echo "$out" | tail -n 1)"
  echo "$out" | grep -iE "reclaimed|freed|vacuum" | tail -n 2 | sed 's/^/    /' | { [ "$QUIET" = 1 ] && cat >/dev/null || cat; }
  return 0
}

# find_delete <what> <find args...>: delete what find matches (or only count it with --dry-run)
find_delete() {
  local what="$1"; shift
  local n; n="$(find "$@" -print 2>/dev/null | wc -l | tr -d ' ')"
  [ "${n:-0}" -gt 0 ] || return 0
  if [ "$DRY" = 1 ]; then say "  would delete $n $what"; else find "$@" -delete 2>/dev/null; say "  deleted $n $what"; fi
}

# truncate_big <what> <MB> <find args...>: empty (not delete) oversized log files that a process may still hold open
truncate_big() {
  local what="$1" mb="$2"; shift 2
  local f
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    if [ "$DRY" = 1 ]; then say "  would empty $what: $f ($(human $(( $(stat -c %s "$f" 2>/dev/null || echo 0) / 1024 ))))"
    else truncate -s 0 "$f" 2>/dev/null && say "  emptied $what: $f"; fi
  done < <(find "$@" -type f -size +"${mb}"M 2>/dev/null)
}

BEFORE="$(used_kb)"
say "candyresolver host cleanup $([ "$DRY" = 1 ] && echo '(DRY RUN: nothing is changed)')"
say "disk before: $(df -h "$ROOT_FS" 2>/dev/null | awk 'NR==2{print $3" used, "$4" free ("$5")"}')"

# ---------------------------------------------------------------- 1. Docker (never volumes)
if want docker && have docker && docker info >/dev/null 2>&1; then
  say "== Docker: stopped containers, unused images, build cache (volumes are never touched)"
  run docker container prune -f --filter "until=$DOCKER_AGE"
  run docker image prune -f
  [ "$AGGR" = 1 ] && run docker image prune -a -f --filter "until=$DOCKER_AGE"
  if [ "$AGGR" = 1 ]; then run docker builder prune -a -f --filter "until=$DOCKER_AGE"; else run docker builder prune -f --filter "until=$DOCKER_AGE"; fi
  run docker network prune -f
  # containers started without log rotation can have gigantic logs (our compose file rotates its own)
  mapfile -t dlogs < <(ls -d $DOCKER_LOG_GLOB 2>/dev/null | head -n 500)
  [ "${#dlogs[@]}" -gt 0 ] && truncate_big "Docker container log" "$DOCKER_LOG_MB" "${dlogs[@]}"
fi

# ---------------------------------------------------------------- 2. systemd journal
if want journal && have journalctl; then
  say "== System journal: keep at most $JOURNAL_SIZE / $JOURNAL_DAYS days"
  run journalctl --vacuum-size="$JOURNAL_SIZE"
  run journalctl --vacuum-time="${JOURNAL_DAYS}d"
fi

# ---------------------------------------------------------------- 3. apt
if want apt && have apt-get; then
  say "== apt: downloaded packages and packages nothing needs any more"
  run apt-get -y clean
  run apt-get -y autoremove --purge
fi

# ---------------------------------------------------------------- 4. snap: old disabled revisions
if want snap && have snap; then
  say "== snap: old revisions"
  while read -r name rev; do [ -n "$name" ] && run snap remove "$name" --revision="$rev"; done < <(snap list --all 2>/dev/null | awk '/disabled/{print $1, $3}')
fi

# ---------------------------------------------------------------- 5. logs
if want logs; then
  say "== Logs in $LOG_DIR: rotated/compressed files older than $LOG_DAYS days, oversized logs"
  find_delete "old rotated log file(s)" "$LOG_DIR" -xdev -type f \( -name '*.gz' -o -name '*.xz' -o -name '*.bz2' -o -name '*.old' -o -name '*.[0-9]' \) -mtime +"$LOG_DAYS"
  truncate_big "oversized log" "$BIG_LOG_MB" "$LOG_DIR" -maxdepth 2 -xdev -not -path '*/journal/*'
fi

# ---------------------------------------------------------------- 6. temp folders
if want tmp; then
  say "== Temp folders: files not used for $TMP_DAYS days"
  for d in $TMP_DIRS; do
    [ -d "$d" ] || continue
    find_delete "old temp file(s) in $d" "$d" -xdev -type f -atime +"$TMP_DAYS" -mtime +"$TMP_DAYS" -not -path '*/systemd-private-*'
    find_delete "empty temp folder(s) in $d" "$d" -mindepth 1 -xdev -type d -empty -mtime +"$TMP_DAYS" -not -path '*/systemd-private-*'
  done
fi

# ---------------------------------------------------------------- 7. caches and trash (root and the sudo user)
if want cache; then
  say "== Caches: pip, npm, thumbnails, trash"
  homes="/root"; [ -n "${SUDO_USER:-}" ] && homes="$homes $(getent passwd "$SUDO_USER" 2>/dev/null | cut -d: -f6)"
  [ -n "${HC_HOMES:-}" ] && homes="$HC_HOMES"
  for h in $homes; do
    [ -d "$h" ] || continue
    for c in "$h/.cache/pip" "$h/.cache/thumbnails" "$h/.npm/_cacache" "$h/.local/share/Trash"; do
      [ -d "$c" ] || continue
      find_delete "item(s) in $c" "$c" -mindepth 1 -xdev
    done
  done
fi

# ---------------------------------------------------------------- 8. crash dumps
if want crash; then
  say "== Crash dumps older than 3 days"
  find_delete "crash dump(s)" /var/crash /var/lib/systemd/coredump -xdev -type f -mtime +3
fi

# ---------------------------------------------------------------- 9. the app's own clean (orphan library folders, old script/browser temp ...)
if [ "$APP" = 1 ]; then
  say "== App: asking candyresolver to clean its own leftovers"
  if [ -z "${CANDY_ADMIN_TOKEN:-}" ]; then warn "--app-clean needs CANDY_ADMIN_TOKEN"; elif [ "$DRY" = 1 ]; then say "  would POST ${CANDY_URL:-http://localhost:8000}/admin/disk/clean"
  else curl -fsS -m 300 -X POST -H "X-Admin-Token: $CANDY_ADMIN_TOKEN" -H 'Content-Type: application/json' -d '{"database":true}' \
         "${CANDY_URL:-http://localhost:8000}/admin/disk/clean" | sed -n 's/.*"freed_bytes":\([0-9]*\).*/  app freed \1 bytes/p' || warn "the app did not answer"; fi
fi

# ---------------------------------------------------------------- 10. give the freed blocks back to the SSD
if want trim && have fstrim && [ "$DRY" = 0 ]; then run fstrim -a; fi

# ---------------------------------------------------------------- summary
AFTER="$(used_kb)"
FREED=$(( ${BEFORE:-0} - ${AFTER:-0} )); [ "$FREED" -lt 0 ] && FREED=0
say "disk after:  $(df -h "$ROOT_FS" 2>/dev/null | awk 'NR==2{print $3" used, "$4" free ("$5")"}')"
if [ "$DRY" = 1 ]; then echo "Dry run finished: nothing was changed. Run without --dry-run to clean."
else echo "$(date '+%F %T') host cleanup done: freed $(human "$FREED"); $(df -h "$ROOT_FS" 2>/dev/null | awk 'NR==2{print $4" free ("$5" used)"}')"; fi

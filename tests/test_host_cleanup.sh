#!/usr/bin/env bash
# Runs scripts/host_cleanup.sh on a fake tree (no Docker / apt / journal): dry run changes nothing, real run cleans the right things only.
# Run: bash tests/test_host_cleanup.sh
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"; S="$HERE/scripts/host_cleanup.sh"
T="$(mktemp -d)"; ok=1
pass() { echo "PASS $1"; }; fail() { echo "FAIL $1"; ok=0; }
chk() { if eval "$2"; then pass "$1"; else fail "$1"; fi; }

mkdir -p "$T/tmp/systemd-private-x" "$T/tmp/olddir" "$T/tmp/keepdir" "$T/log/journal" "$T/home/.cache/pip" "$T/home/.local/share/Trash"
touch -d '10 days ago' "$T/tmp/old.txt" "$T/tmp/systemd-private-x/old.txt" "$T/tmp/olddir"
echo new > "$T/tmp/new.txt"; echo keep > "$T/tmp/keepdir/new.txt"
touch -d '20 days ago' "$T/log/a.log.1" "$T/log/b.gz"; touch "$T/log/c.gz"
truncate -s 600M "$T/log/syslog"; truncate -s 600M "$T/log/journal/x.journal"; echo small > "$T/log/app.log"
echo x > "$T/home/.cache/pip/wheel"; echo y > "$T/home/.local/share/Trash/file"

run() { HC_ALLOW_NONROOT=1 HC_TMP_DIRS="$T/tmp" HC_LOG_DIR="$T/log" HC_HOMES="$T/home" HC_ROOT_FS="$T" bash "$S" --skip docker,journal,apt,snap,crash,trim "$@"; }

out="$(run --dry-run 2>&1)"
chk "dry run says so" '[[ "$out" == *"DRY RUN"* && "$out" == *"nothing was changed"* ]]'
chk "dry run lists what it would do" '[[ "$out" == *"would delete 2 old rotated log"* && "$out" == *"would empty oversized log"* ]]'
chk "dry run changes nothing" '[ -e "$T/tmp/old.txt" ] && [ -e "$T/log/a.log.1" ] && [ "$(stat -c %s "$T/log/syslog")" -gt 1000 ] && [ -e "$T/home/.cache/pip/wheel" ]'

out="$(run 2>&1)"
chk "old temp file removed" '[ ! -e "$T/tmp/old.txt" ]'
chk "new temp file kept" '[ -e "$T/tmp/new.txt" ] && [ -e "$T/tmp/keepdir/new.txt" ]'
chk "systemd-private temp left alone" '[ -e "$T/tmp/systemd-private-x/old.txt" ]'
chk "old empty temp folder removed, the temp root itself stays" '[ ! -e "$T/tmp/olddir" ] && [ -d "$T/tmp" ]'
chk "old rotated logs removed, recent one kept" '[ ! -e "$T/log/a.log.1" ] && [ ! -e "$T/log/b.gz" ] && [ -e "$T/log/c.gz" ]'
chk "oversized log emptied (not deleted)" '[ -e "$T/log/syslog" ] && [ "$(stat -c %s "$T/log/syslog")" = 0 ]'
chk "journal folder is never touched" '[ "$(stat -c %s "$T/log/journal/x.journal")" -gt 1000 ]'
chk "small log kept" '[ "$(cat "$T/log/app.log")" = small ]'
chk "caches and trash emptied, folders stay" '[ ! -e "$T/home/.cache/pip/wheel" ] && [ -d "$T/home/.cache/pip" ] && [ ! -e "$T/home/.local/share/Trash/file" ]'
chk "summary line printed" '[[ "$out" == *"host cleanup done: freed"* ]]'
chk "second run finds nothing more" '[[ "$(run 2>&1)" != *"deleted"* ]]'
chk "--quiet prints just the summary" '[ "$(run --quiet 2>&1 | wc -l)" = 1 ]'
chk "unknown option is refused" '! bash "$S" --nonsense >/dev/null 2>&1'
chk "--help works" 'bash "$S" --help | grep -q dry-run'
chk "never touches Docker volumes: the script has no volume prune" '! grep -E "^[^#]*docker (volume|system) prune" "$S"'

# --install writes a cron file that points at this script; --uninstall removes it
CRON=/etc/cron.d/candyresolver-host-cleanup
if [ "$(id -u)" = 0 ] && [ -d /etc/cron.d ]; then
  bash "$S" --install --aggressive >/dev/null; chk "install writes cron + logrotate" 'grep -q "17 4 \* \* 0 root $S --quiet --aggressive" $CRON && [ -f /etc/logrotate.d/candyresolver-host-cleanup ]'
  bash "$S" --install --daily >/dev/null;      chk "--daily changes the schedule" 'grep -q "17 4 \* \* \* root" $CRON'
  bash "$S" --uninstall >/dev/null;            chk "uninstall removes both files" '[ ! -e $CRON ] && [ ! -e /etc/logrotate.d/candyresolver-host-cleanup ]'
else echo "SKIP cron install test (needs root and /etc/cron.d)"; fi
rm -rf "$T"
[ $ok = 1 ] && echo "ALL OK" || { echo "SOME FAILED"; exit 1; }

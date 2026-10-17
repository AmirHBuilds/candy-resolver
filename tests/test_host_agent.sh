#!/usr/bin/env bash
# The host agent on a fake tree: heartbeat, request handling (whitelisted flags only), result files, dry run, junk requests.
# Run: bash tests/test_host_agent.sh
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"; S="$HERE/scripts/host_cleanup.sh"
T="$(mktemp -d)"; ok=1; A="$T/hostclean"
pass() { echo "PASS $1"; }; fail() { echo "FAIL $1"; ok=0; }
chk() { if eval "$2"; then pass "$1"; else fail "$1"; fi; }
mkdir -p "$T/tmp" "$T/log"; touch -d '10 days ago' "$T/tmp/old.txt"; echo new > "$T/tmp/new.txt"
tick() { HC_AGENT_DIR="$A" HC_ALLOW_NONROOT=1 HC_TMP_DIRS="$T/tmp" HC_LOG_DIR="$T/log" HC_HOMES="$T/none" HC_ROOT_FS="$T" HC_SKIP=docker,journal,apt,snap,crash,trim,cache bash "$S" --agent-tick; }
j() { python3 -c "import json,sys; d=json.load(open('$A/$1')); print(eval(sys.argv[1]))" "$2" 2>/dev/null; }

tick
chk "heartbeat: status.json is valid JSON with a recent alive_at" '[ "$(j status.json "abs(__import__(\"time\").time()-d[\"agent\"][\"alive_at\"])<5")" = True ]'
chk "no request -> nothing runs, no last result yet" '[ "$(j status.json "d[\"last\"] is None and d[\"running\"] is False")" = True ] && [ -e "$T/tmp/old.txt" ]'

echo '{"id": "abc123def456", "aggressive": false, "dry_run": true, "at": 1}' > "$A/request.json"; tick
chk "dry-run request: files untouched, request consumed" '[ -e "$T/tmp/old.txt" ] && [ ! -e "$A/request.json" ]'
chk "dry-run result: id, dry_run flag, would-delete lines in the log" '[ "$(j last.json "d[\"id\"]==\"abc123def456\" and d[\"dry_run\"] is True and d[\"ok\"] is True and any(\"would delete\" in l for l in d[\"log\"])")" = True ]'
chk "status.json carries the last result" '[ "$(j status.json "d[\"last\"][\"id\"]")" = abc123def456 ]'

echo '{"id": "0123456789ab", "aggressive": false, "dry_run": false}' > "$A/request.json"; tick
chk "real request: old temp file cleaned, new one kept" '[ ! -e "$T/tmp/old.txt" ] && [ -e "$T/tmp/new.txt" ]'
chk "real result: not a dry run, ok, log tail present" '[ "$(j last.json "d[\"id\"]==\"0123456789ab\" and d[\"dry_run\"] is False and d[\"ok\"] is True and len(d[\"log\"])>0 and d[\"freed_kb\"]>=0")" = True ]'
chk "result files are world-readable (the container reads them)" '[ "$(stat -c %a "$A/status.json")" = 644 ] && [ "$(stat -c %a "$A/last.json")" = 644 ]'

touch -d '10 days ago' "$T/tmp/old2.txt"
printf '{"id": "x; rm -rf /", "aggressive": "$(touch %s/pwned)", "dry_run": false, "cmd": "touch %s/pwned2"}' "$T" "$T" > "$A/request.json"; tick
chk "junk / injection in the request: nothing from it is executed" '[ ! -e "$T/pwned" ] && [ ! -e "$T/pwned2" ]'
chk "junk request: id falls back to 'unknown', flags stay off, still a normal safe clean" '[ "$(j last.json "d[\"id\"]==\"unknown\" and d[\"aggressive\"] is False")" = True ]'
head -c 300000 /dev/urandom > "$A/request.json"; tick
chk "binary garbage request is survived" '[ ! -e "$A/request.json" ] && [ "$(j status.json "d[\"agent\"][\"version\"]")" = 1 ]'

# the agent must not run two cleanups at once: while the lock is held it only shows that it is alive
echo '{"id": "aaaaaaaaaaaa"}' > "$A/request.json"
( exec 8>"$A/.agent.lock"; flock 8; sleep 3 ) & sleep 0.5
tick
chk "while busy: request stays pending, status says running" '[ -e "$A/request.json" ] && [ "$(j status.json "d[\"running\"]")" = True ]'
wait; tick
chk "after the lock is free the pending request is processed" '[ ! -e "$A/request.json" ] && [ "$(j status.json "d[\"last\"][\"id\"]")" = aaaaaaaaaaaa ]'

# install / uninstall
if [ "$(id -u)" = 0 ] && [ -d /etc/cron.d ]; then
  HC_AGENT_DIR="$A" bash "$S" --install-agent >/dev/null 2>&1; chk "install-agent writes the cron job for that folder" 'grep -q "^\* \* \* \* \* root HC_AGENT_DIR=$A .*--agent-tick" /etc/cron.d/candyresolver-host-agent'
  bash "$S" --uninstall-agent >/dev/null;                     chk "uninstall-agent removes it" '[ ! -e /etc/cron.d/candyresolver-host-agent ]'
else echo "SKIP agent install test"; fi
rm -rf "$T"; [ $ok = 1 ] && echo "ALL OK" || { echo "SOME FAILED"; exit 1; }

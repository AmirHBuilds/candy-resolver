#!/usr/bin/env bash
# Run AFTER deploying:  ./deploy_check.sh http://localhost:8000 YOUR_ADMIN_TOKEN
# Checks that the service is up, the database was migrated and every new endpoint exists. Changes nothing.
BASE="${1:-http://localhost:8000}"; TOKEN="$2"; fail=0
ok()  { echo "  OK    $1"; }
bad() { echo "  FAIL  $1"; fail=1; }
echo "Checking $BASE"
curl -fsS "$BASE/health" >/dev/null && ok "/health" || bad "/health (is the container up? docker compose logs api)"
SPEC=$(curl -fsS "$BASE/openapi.json") || { bad "/openapi.json"; exit 1; }
for p in '/v1/library/{item_id}/audio' '/v1/tasks/{task_id}/sources' '/v1/resolve' '/v1/tasks/{task_id}'; do
  echo "$SPEC" | grep -qF "\"$p\"" && ok "endpoint $p" || bad "endpoint $p missing"
done
for f in max_sources expires_at has_more sources_found error_code duration_s; do
  echo "$SPEC" | grep -q "\"$f\"" && ok "field $f" || bad "field $f missing in openapi"
done
if [ -n "$TOKEN" ]; then
  S=$(curl -fsS -H "X-Admin-Token: $TOKEN" "$BASE/admin/sources") && ok "admin API (database migrated)" || bad "admin API / token"
  echo "$S" | grep -q '"rank"' && ok "sources have rank + response times" || { [ "$S" = "[]" ] && ok "no sources yet" || bad "rank missing"; }
else
  echo "  (skipped admin checks: pass the admin token as 2nd argument)"
fi
[ $fail = 0 ] && echo "ALL GOOD" || { echo "SOMETHING FAILED: docker compose logs --tail 100 api"; exit 1; }

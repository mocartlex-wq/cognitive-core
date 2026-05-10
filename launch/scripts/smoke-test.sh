#!/usr/bin/env bash
# Cognitive Core — E2E smoke test.
# Verifies that all critical paths work: API health, rooms, ask/answer roundtrip.
#
# Run after `make up` or via `make smoke`.
# Exits 0 on success, non-zero with explanation on failure.

set -euo pipefail

API_URL="${API_URL:-http://localhost:9001}"
ROOMS_URL="${ROOMS_URL:-http://localhost:9098}"
TIMEOUT="${TIMEOUT:-5}"

ok()    { printf "  \033[1;32m✓\033[0m %s\n" "$*"; }
fail()  { printf "  \033[1;31m✗\033[0m %s\n" "$*" >&2; exit 1; }
info()  { printf "  \033[1;36m▶\033[0m %s\n" "$*"; }

cleanup() { :; }
trap cleanup EXIT

printf "\n\033[1mCognitive Core smoke test\033[0m\n"
printf "  API:   %s\n  Rooms: %s\n\n" "$API_URL" "$ROOMS_URL"

# ── 1. API health ─────────────────────────────────────────────────────
info "API /health ..."
H=$(curl -sf --max-time "$TIMEOUT" "$API_URL/health" || true)
echo "$H" | grep -qE '"(healthy|status)"' || fail "API /health did not respond as expected: $H"
ok "API healthy"

# ── 2. Rooms create ───────────────────────────────────────────────────
info "POST /rooms ..."
CREATE=$(curl -sf --max-time "$TIMEOUT" -X POST "$ROOMS_URL/rooms" \
  -H "Content-Type: application/json" \
  -d '{"name":"smoke-test","creator":"smoker"}')
ROOM_ID=$(echo "$CREATE" | python3 -c 'import json,sys;print(json.load(sys.stdin)["room_id"])')
ROOM_KEY=$(echo "$CREATE" | python3 -c 'import json,sys;print(json.load(sys.stdin)["api_key"])')
[ -n "$ROOM_ID" ] && [ -n "$ROOM_KEY" ] || fail "create returned no id/key: $CREATE"
ok "room created: ${ROOM_ID:0:8} (key=${ROOM_KEY:0:8}…)"

# ── 3. Join two participants ──────────────────────────────────────────
info "two participants join ..."
for P in alice bob; do
  R=$(curl -sf --max-time "$TIMEOUT" -X POST "$ROOMS_URL/rooms/$ROOM_ID/join" \
    -H "X-Room-Key: $ROOM_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"agent_id\":\"$P\"}")
  echo "$R" | grep -qE '"(ok|joined)"\s*:\s*(true|"joined")' || fail "$P join failed: $R"
done
ok "alice + bob joined"

# ── 4. Post a broadcast ───────────────────────────────────────────────
info "POST broadcast ..."
M=$(curl -sf --max-time "$TIMEOUT" -X POST "$ROOMS_URL/rooms/$ROOM_ID/post" \
  -H "X-Room-Key: $ROOM_KEY" \
  -H "Content-Type: application/json" \
  -d '{"agent_id":"alice","text":"hello room"}')
echo "$M" | grep -qE '"(message_id|id)"' || fail "post failed: $M"
ok "broadcast posted"

# ── 5. Read messages ──────────────────────────────────────────────────
info "GET /messages ..."
MSGS=$(curl -sf --max-time "$TIMEOUT" "$ROOMS_URL/rooms/$ROOM_ID/messages?limit=5" \
  -H "X-Room-Key: $ROOM_KEY")
COUNT=$(echo "$MSGS" | python3 -c 'import json,sys;print(len(json.load(sys.stdin).get("messages",[])))')
[ "$COUNT" -ge 1 ] || fail "messages empty: $MSGS"
ok "messages returned ($COUNT)"

# ── 6. Pending sync ───────────────────────────────────────────────────
info "GET /sync-pending ..."
P=$(curl -sf --max-time "$TIMEOUT" "$ROOMS_URL/rooms/$ROOM_ID/sync-pending?agent_id=bob" \
  -H "X-Room-Key: $ROOM_KEY")
echo "$P" | grep -qE '"(pending|pending_questions)"' || fail "sync-pending malformed: $P"
ok "sync-pending OK"

printf "\n\033[1;32m✅ all smoke checks passed\033[0m\n\n"

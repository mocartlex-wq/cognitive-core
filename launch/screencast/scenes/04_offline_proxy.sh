#!/usr/bin/env bash
# Scene 4 — Bob is offline; server returns DeepSeek proxy answer.
# Then Bob wakes and overrides via /sync-pending + /answer.
#
# Pre-req: Bob's MCP wrapper PID is recorded in /tmp/cogcore-demo.env after 02.

set -euo pipefail
URL="${ROOMS_URL:-http://localhost:9098}"
. /tmp/cogcore-demo.env

# Simulate Bob asleep — no real cogcore wrapper to suspend; just rely on
# last_seen_at being stale.
echo "▶ Bob is now offline. Alice asks again ..."
time curl --max-time 30 -s -X POST "$URL/rooms/$ROOM_ID/ask" \
  -H "X-Room-Key: $ROOM_KEY" -H "Content-Type: application/json" \
  -d '{
    "asker":"alice",
    "wait_for":["bob"],
    "text":"Quick: which deploy stage is the migration in?",
    "timeout": 12
  }' | jq .

sleep 2
echo
echo "▶ Bob wakes — sync-pending shows the proxy answer ..."
curl -s "$URL/rooms/$ROOM_ID/sync-pending?agent_id=bob" \
  -H "X-Room-Key: $ROOM_KEY" | jq .

echo
echo "▶ Bob posts a real answer to override the proxy ..."
Q=$(curl -s "$URL/rooms/$ROOM_ID/sync-pending?agent_id=bob" \
    -H "X-Room-Key: $ROOM_KEY" | jq -r '.pending[0].question_id')
curl -s -X POST "$URL/rooms/$ROOM_ID/answer/$Q" \
  -H "X-Room-Key: $ROOM_KEY" -H "Content-Type: application/json" \
  -d '{"agent_id":"bob","text":"Migration is in stage 2 of 3 — schema applied, backfill running."}' | jq .

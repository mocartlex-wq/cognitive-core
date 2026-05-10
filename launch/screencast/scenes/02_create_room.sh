#!/usr/bin/env bash
# Scene 2 — create + join. Stores room state in /tmp/cogcore-demo.env.

set -euo pipefail
URL="${ROOMS_URL:-http://localhost:9098}"

echo "▶ Alice creates a room ..."
R=$(curl -s -X POST "$URL/rooms" \
  -H "Content-Type: application/json" \
  -d '{"name":"design-review","creator":"alice"}')
echo "$R" | jq .
ROOM_ID=$(echo "$R" | jq -r .room_id)
ROOM_KEY=$(echo "$R" | jq -r .api_key)

echo
echo "▶ Bob joins ..."
curl -s -X POST "$URL/rooms/$ROOM_ID/join" \
  -H "X-Room-Key: $ROOM_KEY" -H "Content-Type: application/json" \
  -d '{"agent_id":"bob"}' | jq .

# Persist for downstream scenes.
cat > /tmp/cogcore-demo.env <<ENV
ROOM_ID=$ROOM_ID
ROOM_KEY=$ROOM_KEY
ENV
echo
echo "✓ saved to /tmp/cogcore-demo.env"

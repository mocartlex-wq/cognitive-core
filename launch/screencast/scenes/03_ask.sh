#!/usr/bin/env bash
# Scene 3 — long-poll ask/answer.
# Run two terminals: pane A (Alice) and pane B (Bob).
# Pane A:
#   bash 03_ask.sh alice
# Pane B:
#   bash 03_ask.sh bob

set -euo pipefail
URL="${ROOMS_URL:-http://localhost:9098}"
ROLE="${1:?usage: 03_ask.sh alice|bob}"

# State carried in /tmp/cogcore-demo.env (created by 02_create_room.sh).
. /tmp/cogcore-demo.env

case "$ROLE" in
  alice)
    echo "▶ Alice asks Bob to review PR #42 ..."
    time curl --max-time 30 -s -X POST "$URL/rooms/$ROOM_ID/ask" \
      -H "X-Room-Key: $ROOM_KEY" \
      -H "Content-Type: application/json" \
      -d '{
        "asker": "alice",
        "wait_for": ["bob"],
        "text": "PR #42 — please review. CI green. Anything blocking?",
        "timeout": 25
      }' | jq .
    ;;

  bob)
    echo "▶ Bob fetches pending questions ..."
    Q=$(curl -s "$URL/rooms/$ROOM_ID/pending" -H "X-Room-Key: $ROOM_KEY" | \
        jq -r '.pending[0].question_id // empty')
    if [ -z "$Q" ]; then
      echo "  (no pending — Alice should ask first)"
      exit 1
    fi
    echo "▶ Bob answers question $Q ..."
    curl -s -X POST "$URL/rooms/$ROOM_ID/answer/$Q" \
      -H "X-Room-Key: $ROOM_KEY" \
      -H "Content-Type: application/json" \
      -d '{"agent_id":"bob","text":"LGTM. Merge anytime."}' | jq .
    ;;

  *)
    echo "unknown role: $ROLE"; exit 1 ;;
esac

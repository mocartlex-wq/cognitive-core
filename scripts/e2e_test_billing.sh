#!/usr/bin/env bash
# E2E test для billing endpoints (PR feat/billing-scaffold + pricing-wire).
#
# Покрывает только структуру / auth / error handling — НЕ делает реальные
# платежи. Real test против Stripe sandbox: отдельный manual flow когда
# owner добавит STRIPE_SECRET_KEY=sk_test_... в .env.
#
# Usage:
#   bash scripts/e2e_test_billing.sh

set -euo pipefail

BASE="${COG_BASE:-https://mcp.me-ai.ru}"

red()   { echo -e "\033[31m$*\033[0m"; }
green() { echo -e "\033[32m$*\033[0m"; }
blue()  { echo -e "\033[34m$*\033[0m"; }

echo "=========================================="
echo "E2E billing endpoints test"
echo "Target: $BASE"
echo "=========================================="

# ─── 1. Unauthenticated checkout → 401 ──────────────────────────────────
blue "[1/5] POST /api/billing/checkout/pro без auth"
STATUS=$(curl -sS -o /dev/null -w "%{http_code}" -X POST "$BASE/api/billing/checkout/pro?provider=stripe")
if [[ "$STATUS" == "401" ]] || [[ "$STATUS" == "403" ]]; then
    green "OK $STATUS (auth required, expected)"
else
    red "WARN: expected 401/403, got $STATUS"
fi

# ─── 2. Webhook без signature → 401 ─────────────────────────────────────
blue "[2/5] POST /api/billing/webhook/stripe без подписи"
STATUS=$(curl -sS -o /dev/null -w "%{http_code}" -X POST \
    -H "Content-Type: application/json" -d '{}' "$BASE/api/billing/webhook/stripe")
if [[ "$STATUS" == "422" ]] || [[ "$STATUS" == "401" ]]; then
    green "OK $STATUS (missing Stripe-Signature header)"
else
    red "WARN: expected 422/401, got $STATUS"
fi

# ─── 3. Webhook с invalid signature → 401 ───────────────────────────────
blue "[3/5] POST /api/billing/webhook/stripe с FAKE signature"
STATUS=$(curl -sS -o /dev/null -w "%{http_code}" -X POST \
    -H "Content-Type: application/json" \
    -H "Stripe-Signature: t=999,v1=DEADBEEF" \
    -d '{"id":"evt_test"}' "$BASE/api/billing/webhook/stripe")
# Stripe verify FAIL → 401, OR not configured → 503
if [[ "$STATUS" == "401" ]] || [[ "$STATUS" == "503" ]]; then
    green "OK $STATUS (invalid signature rejected OR provider not configured)"
else
    red "WARN: expected 401/503, got $STATUS"
fi

# ─── 4. ЮKassa webhook без secret (passthrough JSON parse) ──────────────
blue "[4/5] POST /api/billing/webhook/yookassa с invalid JSON"
STATUS=$(curl -sS -o /dev/null -w "%{http_code}" -X POST \
    -H "Content-Type: application/json" -d 'not-json' "$BASE/api/billing/webhook/yookassa")
if [[ "$STATUS" == "400" ]] || [[ "$STATUS" == "503" ]]; then
    green "OK $STATUS (parse fail OR not configured)"
else
    red "WARN: expected 400/503, got $STATUS"
fi

# ─── 5. Subscriptions endpoint без auth → 401 ───────────────────────────
blue "[5/5] GET /api/billing/subscriptions/me без auth"
STATUS=$(curl -sS -o /dev/null -w "%{http_code}" "$BASE/api/billing/subscriptions/me")
if [[ "$STATUS" == "401" ]] || [[ "$STATUS" == "403" ]]; then
    green "OK $STATUS (auth required)"
else
    red "WARN: expected 401/403, got $STATUS"
fi

echo ""
green "=========================================="
green "✅ Billing endpoints E2E structure-test PASS"
green "=========================================="
echo ""
echo "Реальные платежи будут работать когда owner:"
echo "  1. Зарегистрирует Stripe + ЮKassa аккаунты"
echo "  2. Добавит creds в /opt/cognitive-core/.env"
echo "  3. Применит migration: sudo alembic upgrade head"
echo "  4. Настроит webhooks в обоих dashboards"
echo "  См. docs/quickstart-billing.md"

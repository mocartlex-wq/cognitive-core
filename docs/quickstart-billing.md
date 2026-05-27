# Quickstart: Billing setup (Stripe + ЮKassa)

Owner mandate 2026-05-26: «у меня есть MasterCard и VPN» — оба provider'а
доступны. Этот гайд для owner-а (platform operator), не для tenants.

## Архитектура

| Provider | Регион tenant'ов | Карты | Setup time |
|---|---|---|---|
| **Stripe** | EU/US/world | Visa/MasterCard/Apple Pay | ~30 мин |
| **ЮKassa** | РФ | МИР / Visa-РФ / Сбер ID / СБП | ~60 мин (требует ИП/ООО) |

Tenants выбирают provider при upgrade (или платформа route'ит по геолокации).

## Tier pricing (single source of truth)

| Tier | RUB | USD | events/day | storage | agents |
|---|---|---|---|---|---|
| Free | 0 | $0 | 10 000 | 1 GB | 10 |
| Pro | 490₽/мес | $5/mo | 100 000 | 10 GB | 50 |
| Enterprise | custom | custom | 1 000 000 | 1 TB | 500 |

Все в `app/services/billing/__init__.py` (TIER_PRICING_RUB, TIER_PRICING_USD,
TIER_LIMITS). Менять цены — в одном месте.

---

## Setup: Stripe

### 1. Создать аккаунт
- https://dashboard.stripe.com → Sign up
- Нужен MasterCard для verification бизнеса
- Stripe доступен из РФ с VPN (US IP)

### 2. Получить API keys
- Dashboard → Developers → API Keys
- **Secret key** (sk_test_... для sandbox, sk_live_... для production)

### 3. Создать Products + Prices
В Dashboard → Products → New:
- Name: `Cognitive Core Pro`
- Pricing: $5/month recurring → копируй `price_xxxxx`

### 4. Setup webhook
- Dashboard → Developers → Webhooks → Add endpoint
- URL: `https://mcp.me-ai.ru/api/billing/webhook/stripe`
- Events to listen: `checkout.session.completed`, `customer.subscription.deleted`
- Скопируй **Signing secret** (whsec_...)

### 5. Add to server `.env`
```bash
ssh salex@100.81.77.25
sudo nano /opt/cognitive-core/.env
# Добавь:
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PRICE_PRO=price_...
# опц. STRIPE_PRICE_ENTERPRISE=price_... (когда сделаешь enterprise tier в Stripe)

sudo systemctl restart cognitive-api  # или подождать auto-deploy
```

### 6. Test
```bash
# В Stripe Dashboard → Webhooks → твой endpoint → Send test webhook
# checkout.session.completed → должен вернуть 200 OK
# Проверь: SELECT * FROM billing_processed_events WHERE provider='stripe';
```

---

## Setup: ЮKassa

### 1. Регистрация (требует юр.лицо или ИП)
- https://yookassa.ru → Регистрация → выбор организационной формы
- Документы: ИНН, ОГРН/ОГРНИП, расчётный счёт
- Срок approval: ~1-3 рабочих дня (модерация)

### 2. Получить creds
- Личный кабинет → Интеграция → Ключ API
- **shopId** + **Секретный ключ** (test_xxx для sandbox, live_xxx для prod)

### 3. Add to server `.env`
```bash
YOOKASSA_SHOP_ID=1234567
YOOKASSA_SECRET_KEY=live_...
```

### 4. Setup webhook
- Личный кабинет → HTTP-уведомления → Добавить
- URL: `https://mcp.me-ai.ru/api/billing/webhook/yookassa`
- События: `payment.succeeded`, `refund.succeeded`

### 5. (Опц.) Nginx IP whitelist
ЮKassa не подписывает webhooks — fallback на source-IP filter:

В `nginx/conf.d/yookassa-webhook.conf`:
```nginx
location /api/billing/webhook/yookassa {
    # ЮKassa IP whitelist (https://yookassa.ru/developers/using-api/webhooks)
    allow 185.71.76.0/27;
    allow 185.71.77.0/27;
    allow 77.75.153.0/25;
    allow 77.75.154.128/25;
    allow 77.75.156.11/32;
    allow 77.75.156.35/32;
    deny all;
    proxy_pass http://cognitive_api/api/billing/webhook/yookassa;
}
```

### 6. Test
- Создай test payment через API:
  ```bash
  curl -X POST -u 1234567:test_xxx \
    -H "Idempotence-Key: $(uuidgen)" \
    -d '{"amount":{"value":"10.00","currency":"RUB"},"payment_method_data":{"type":"bank_card"},"confirmation":{"type":"redirect","return_url":"https://example.com"},"capture":true}' \
    https://api.yookassa.ru/v3/payments
  ```
- Выводимый `confirmation_url` → открой → введи тестовую карту `5555 5555 5555 4444`

---

## Owner action checklist

- [ ] Stripe account зарегистрирован + verified
- [ ] STRIPE_PRICE_PRO создан в Dashboard
- [ ] STRIPE_WEBHOOK_SECRET добавлен в `.env`
- [ ] ЮKassa account зарегистрирован (юр.лицо одобрено)
- [ ] YOOKASSA_SHOP_ID + SECRET добавлены в `.env`
- [ ] Webhooks настроены в обоих dashboards
- [ ] Migration 0014 применена: `alembic upgrade head`
- [ ] Test checkout прошёл через тестовую карту
- [ ] First real payment → проверь `owner_quotas.tier = 'pro'` в БД

---

## Architecture

```
[Tenant clicks "Upgrade Pro" on /ui/pricing]
   ↓
[Frontend → POST /api/billing/checkout/pro?provider=stripe]
   ↓
[app/api/billing.py:create_checkout()]
   ↓
[app/services/billing/stripe_provider.py:create_checkout()]
   ↓
[Stripe API: POST /v1/checkout/sessions → {checkout_url}]
   ↓
[Frontend redirect → Stripe-hosted payment page]
   ↓
[User pays → Stripe → POST webhook to /api/billing/webhook/stripe]
   ↓
[verify_webhook(body, Stripe-Signature) → HMAC check]
   ↓
[handle_event() → UPDATE owner_quotas SET tier='pro', max_events_per_day=100000]
   ↓
[Идемпотентность: INSERT into billing_processed_events]
```

---

## Troubleshooting

| Симптом | Причина | Fix |
|---|---|---|
| `503 Stripe не настроен` | STRIPE_SECRET_KEY отсутствует | Add в `.env` + restart |
| `401 Invalid signature` на webhook | Webhook secret не совпадает | Скопируй заново из Dashboard |
| `400 ЮKassa поддерживает только RUB` | Tenant в USD передал ЮKassa | Route в Stripe вместо |
| Payment succeeded но tier не обновился | Webhook не доходит / IP whitelist слишком строгий | Tail logs `docker logs cognitive_api | grep billing` |
| Дважды списали | webhook retry без idempotency | Должно быть прозрачно — проверь billing_processed_events |

---

## Сопровождение

- Email: support@me-ai.ru
- Stripe issues: https://support.stripe.com/
- ЮKassa issues: https://yookassa.ru/contacts

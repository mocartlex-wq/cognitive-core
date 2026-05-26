# Внешние AI-провайдеры (per-tenant keys)

Cognitive Core поддерживает **opt-in** подключение твоих собственных API-ключей
к внешним AI-провайдерам для vision-анализа видео. Платформа использует **твой**
ключ — оплата идёт на **твой** счёт у провайдера.

## Зачем это нужно

По умолчанию платформа использует:
1. Shared Qwen-VL (Alibaba) — оплачивается платформой
2. DeepSeek text-only fallback — если Qwen недоступен

Если нужна более качественная или специализированная vision-обработка
(например, MiniMax / Claude для отдельных задач, GigaChat для compliance),
подключай свой ключ — vision_analyzer автоматически использует его первым.

## Где настроить

1. Логин на [https://mcp.me-ai.ru/ui/profile](https://mcp.me-ai.ru/ui/profile)
2. Карточка **«🤖 Внешние AI-провайдеры»**
3. Разверни нужного provider'а → введи ключ → **Сохранить** → **Проверить**

## Provider preference order

Если у тебя подключено несколько ключей, vision_analyzer пробует их в этом
порядке (первый успешный wins):

| # | Provider | Label | Где взять ключ | Стоимость на 12-frame video |
|---|----------|-------|----------------|------------------------------|
| 1 | `qwen` | Qwen-VL (Alibaba) | [DashScope console](https://dashscope.console.aliyuncs.com/apiKey) | ~$0.02-0.03 |
| 2 | `minimax` | MiniMax (Hailuo) | [MiniMax platform](https://www.minimax.io/platform/keys) | ~$0.04-0.08 |
| 3 | `gigachat` | GigaChat (Sber) | [Sber Developers](https://developers.sber.ru/portal/products/gigachat-api) | ~10₽ |
| 4 | `claude` | Claude Haiku (Anthropic) | [Anthropic Console](https://console.anthropic.com/settings/keys) | ~$0.005-0.015 |
| 5 | `openai` | GPT-4o-mini (OpenAI) | [OpenAI Platform](https://platform.openai.com/api-keys) | ~$0.01-0.02 |
| 6 | `gemini` | Gemini 2.0 Flash (Google) | [Google AI Studio](https://ai.google.dev/) | ~$0.001-0.005 |

Если все твои ключи failed (auth_failed / rate_limit / network), fallback
прозрачно срабатывает на shared Qwen + DeepSeek (как раньше).

## Какие quality-результаты ожидать

- **Qwen-VL** — лучший для русского transcript-context, хорошо описывает интерфейсы
- **MiniMax** — visual reasoning (что именно показано на кадрах + анализ deltas)
- **Claude Haiku** — стабильный, короткий, чёткий
- **GPT-4o-mini** — баланс цена/качество для разнообразных видео
- **Gemini 2.0 Flash** — fastest (1-3 сек typically), хорошо для коротких клипов
- **GigaChat** — РФ-резидентный (для compliance-чувствительных tenants)

## Безопасность

- **Encrypt at rest:** ключи шифруются Fernet (symmetric AES-128 + HMAC) при
  сохранении в БД. Plaintext не хранится никогда.
- **Не светим в UI:** при отображении показываются только первые 4 + последние 4
  символа (`sk-x...XYZ7`), даже в /admin интерфейсе.
- **Не логируем:** plaintext ключи никогда не попадают в logs (даже в DEBUG-mode
  и в exception messages).
- **Audit-trail:** каждое использование твоего ключа пишется в L1
  (`domain=external_key_usage`), включая provider / model / tokens.
  Доступно через `cognitive_recall(domain='external_key_usage')`.

## Override base_url / model_name

В UI разверни секцию «Дополнительно» при сохранении — можно задать:

- **base_url** — например `https://api.openai.com/v1` (или твой proxy)
- **model_name** — например `claude-sonnet-4-5` вместо `claude-haiku-4-5`

Это позволяет:
- Использовать региональные endpoints (EU / US / CN)
- Выбирать конкретную модель (cheap-Haiku vs strong-Opus)
- Включать корпоративные proxy / OpenAI-compatible self-hosted (Ollama, LM Studio)

## API endpoints

Если ты пишешь свой клиент:

```http
GET    /user/settings/external-keys
       → { items: [{provider, masked_key, last_test_status, ...}] }

POST   /user/settings/external-key
       Body: { provider, api_key, base_url?, model_name? }
       → { ok, masked_key }

POST   /user/settings/external-key/{provider}/test
       → { ok, message, latency_ms, status }

DELETE /user/settings/external-key/{provider}
       → { ok }
```

Все требуют валидную session-cookie (логин через magic-link на /ui/login).

## FAQ

**Q: Что делать, если test показывает auth_failed?**
A: Ключ невалиден или истёк. Создай новый на портале provider'а, обнови через
   /ui/profile (UPSERT — старое значение перезапишется).

**Q: Сколько ключей можно хранить?**
A: По одному на провайдера (PRIMARY KEY = owner_user_id + provider). Всего —
   максимум 6 (Qwen, MiniMax, GigaChat, Claude, OpenAI, Gemini).

**Q: Удалили мой аккаунт. Что с ключами?**
A: CASCADE — все ключи автоматически удаляются при удалении аккаунта.

**Q: Где увидеть сколько потратил на vision?**
A: `cognitive_recall(domain='external_key_usage', limit=50)` через MCP — увидишь
   все вызовы с tokens_in / tokens_out / provider.

**Q: Если у меня нет ни одного ключа?**
A: Vision-analyzer fallback на shared Qwen (если platform admin его настроил)
   или DeepSeek text-only. Никаких ошибок — всё работает.

## Что НЕ делает платформа

- НЕ создаёт shared platform-keys для MiniMax / Claude / OpenAI / Gemini —
  только opt-in per-tenant. Только Qwen имеет shared fallback (платный для
  владельца платформы).
- НЕ интегрируется со Stripe / биллингом для расхода — платишь сам напрямую
  провайдеру.
- НЕ кэширует ответы между tenant'ами — каждый запрос идёт на твой ключ.

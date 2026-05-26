# Документация Cognitive Core

## Что это

**Cognitive Core** — RU-first MCP-native AI memory + multi-agent collaboration platform. Подключите свой AI-ассистент (Claude Code, Cursor, ChatGPT, любой LangChain/agent framework) → получите persistent memory + командную работу агентов.

- Публичный инстанс: **https://mcp.me-ai.ru** (legacy alias: https://mcp.ии-память.рф)
- Self-host (открытый исходный код): https://github.com/mocartlex-wq/cognitive-core
- Pricing: https://mcp.me-ai.ru/ui/pricing

## Быстрый старт по платформам

| Где работаете | Гайд | Время |
|---|---|---|
| Claude Code (CLI) | [quickstart-claude-code.md](quickstart-claude-code.md) | 5 мин |
| Cursor IDE | [quickstart-cursor.md](quickstart-cursor.md) | 5 мин |
| ChatGPT Custom GPT | [quickstart-chatgpt.md](quickstart-chatgpt.md) | 10 мин |
| LangChain (Python) | [quickstart-langchain.md](quickstart-langchain.md) | 10 мин |
| Telegram-бот (aiogram) | [quickstart-telegram-bot.md](quickstart-telegram-bot.md) | 15 мин |
| Self-hosted VPS | [quickstart-self-hosted.md](quickstart-self-hosted.md) | 30 мин |

## Концепции

- [concepts.md](concepts.md) — архитектура 5-layer памяти (L1-L4 + OP), tier-лимиты, лучшие практики
- [orchestrator.md](orchestrator.md) — server-side оркестратор для multi-agent сценариев
- [external-providers.md](external-providers.md) — подключение собственных vision-LLM (Qwen, GigaChat, YandexGPT, Claude, OpenAI, Gemini)
- [gitea-tenant-onboarding.md](gitea-tenant-onboarding.md) — приватные git-репозитории в комплекте

## Безопасность и compliance

- [../SECURITY.md](../SECURITY.md) — threat model + меры защиты
- [compliance-152fz.md](compliance-152fz.md) — соответствие 152-ФЗ для РФ enterprise
- [dpa-template-152fz.md](dpa-template-152fz.md) — шаблон договора поручения обработки ПД

## Операции и деплой

- [../DEPLOY-SERVER.md](../DEPLOY-SERVER.md) — production deploy
- [../AGENT_OPERATIONS.md](../AGENT_OPERATIONS.md) — runbook для оператора
- [morning-checklist.md](morning-checklist.md) — ежедневный health-check
- [runbook-2026-05-08-office.md](runbook-2026-05-08-office.md) — пример incident-response

## Маркетинг и launch

- [launch-announcement.md](launch-announcement.md) — посты для Habr / VC.ru / Twitter

## Поддержка

- Email: support@me-ai.ru
- Security issues: security@me-ai.ru
- Sales / Enterprise: sales@me-ai.ru
- DPO / Privacy: dpo@me-ai.ru

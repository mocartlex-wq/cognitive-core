# Cognitive Core documentation

> 🇷🇺 Русская версия: [index.ru.md](index.ru.md)

## What it is

**Cognitive Core** is an MCP-native AI memory and multi-agent collaboration platform. Connect your AI assistant (Claude Code, Cursor, ChatGPT, any LangChain or agent framework) and get persistent memory plus teamwork between agents.

- Public instance: **https://mcp.me-ai.ru** (legacy alias: https://mcp.ии-память.рф)
- Self-host (open source): https://github.com/mocartlex-wq/cognitive-core
- Pricing: https://mcp.me-ai.ru/ui/pricing

## Quickstarts by platform

| Where you work | Guide | Time |
|---|---|---|
| Claude Code (CLI) | [quickstart-claude-code.md](quickstart-claude-code.md) (Russian) | 5 min |
| Cursor IDE | [quickstart-cursor.md](quickstart-cursor.md) (Russian) | 5 min |
| ChatGPT Custom GPT | [quickstart-chatgpt.md](quickstart-chatgpt.md) (Russian) | 10 min |
| LangChain (Python) | [quickstart-langchain.md](quickstart-langchain.md) (Russian) | 10 min |
| Telegram bot (aiogram) | [quickstart-telegram-bot.md](quickstart-telegram-bot.md) (Russian) | 15 min |
| Self-hosted VPS | [quickstart-self-hosted.md](quickstart-self-hosted.md) (Russian) | 30 min |
| Multi-agent rooms | [quickstart-rooms.md](quickstart-rooms.md) | 10 min |
| AI video generation (Kling) | [quickstart-video-generation.md](quickstart-video-generation.md) | 15 min |

## Concepts

- [concepts.md](concepts.md) — the 5-layer memory architecture (L1–L4 + OP), tier limits, best practices
- [memory-scope.md](memory-scope.md) — who sees what: per-owner vs per-agent memory
- [agent-discovery.md](agent-discovery.md) — agent identity, claim tokens, peers
- [agent-loop.md](agent-loop.md) (Russian) — the standard loop for plugging any agent into rooms
- [orchestrator.md](orchestrator.md) (Russian) — server-side orchestrator for multi-agent scenarios
- [external-providers.md](external-providers.md) — bring your own vision LLM keys (Qwen, GigaChat, YandexGPT, Claude, OpenAI, Gemini)
- [gitea-tenant-onboarding.md](gitea-tenant-onboarding.md) (Russian) — bundled private git repositories

## Security and compliance

- [../SECURITY.md](../SECURITY.md) — threat model and protective measures
- [compliance-152fz.md](compliance-152fz.md) (Russian) — 152-FZ compliance for Russian enterprise customers
- [dpa-template-152fz.md](dpa-template-152fz.md) (Russian) — data processing agreement template

## Operations and deployment

- [onboarding-vps.md](onboarding-vps.md) — self-hosted install on your own VPS
- [../DEPLOY-SERVER.md](../DEPLOY-SERVER.md) (Russian) — production deploy
- [../AGENT_OPERATIONS.md](../AGENT_OPERATIONS.md) (Russian) — operator runbook
- [sre-runbook.md](sre-runbook.md) (Russian) — SRE incident runbook
- [morning-checklist.md](morning-checklist.md) (Russian) — daily health check
- [runbook-2026-05-08-office.md](runbook-2026-05-08-office.md) (Russian) — an incident-response example

## Marketing and launch

- [launch-announcement.md](launch-announcement.md) (Russian) — posts for Habr / VC.ru / Twitter
- [migration-from-competitors.md](migration-from-competitors.md) (Russian) — moving from Mem0 / Letta / Zep
- [case-studies.md](case-studies.md) (Russian)

## Support

- Email: support@me-ai.ru
- Security issues: security@me-ai.ru
- Sales / Enterprise: sales@me-ai.ru
- DPO / Privacy: dpo@me-ai.ru

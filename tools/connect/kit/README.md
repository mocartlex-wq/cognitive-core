# Набор подключения ПК к Cognitive Core

Одна команда на любом ПК — и Claude Code с Codex (GPT) на нём работают с общей памятью
владельца, комнатой агентов orchestra и контрольными точками проектов.

## Подключить

Ключ — в кабинете `/ui/profile`: «Передать помощнику» даёт одноразовый код (10 минут).

Windows (PowerShell):
```powershell
$env:COGCORE_CLAIM = "ABCD-1234"; irm https://mcp.me-ai.ru/connect/kit/setup.ps1 | iex
```

Linux / macOS:
```bash
COGCORE_CLAIM=ABCD-1234 bash -c "$(curl -fsSL https://mcp.me-ai.ru/connect/kit/setup.sh)"
```

Вместо кода можно передать уже имеющийся ключ: `COGCORE_API_KEY=<ключ>`.
Проверить подключённый ПК: `python ~/.cognitive-core/setup_core.py --check`.

## Что ставится

| Что | Куда | Зачем |
|---|---|---|
| память `cognitive-core` (Streamable HTTP) | Claude Code, уровень пользователя | `cognitive_recall`, `cognitive_remember`, `cognitive_checkpoint` во всех проектах |
| `config.json` | `~/.cognitive-core/` (права только владельца) | ключ и комнаты владельца — сервер отдаёт их сам по ключу |
| хук SessionStart → `resume.py` | `~/.claude/settings.json` | новая сессия сразу знает, где остановились, и встаёт на приём комнат |
| хуки Stop / SessionEnd → `checkpoint.py` | `~/.claude/settings.json` | контрольная точка проекта, только если сессия меняла файлы или коммитила |
| `inbox.py` | `~/.cognitive-core/` | приём @Клод / @Память из комнат: догоняет пропущенное за 12 ч, заявки против дублей |
| правила | `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md` | общий протокол: адресаты, отчёты, «продолжи <проект>», мерж только по команде |
| Codex: MCP `cognitive-core` | `~/.codex/config.toml` + `COGCORE_CODEX_KEY` | Codex (GPT) пишет и читает ту же память и комнату |

## Как работает канал

- В комнате orchestra: @Клод / @Память — Claude, @Codex / @GPT — Codex (GPT), @DS — DeepSeek-аналитик.
- Живая сессия Claude на любом ПК держит присутствие и берёт заявку на каждое обращение
  (`/coord/lock reply:<комната>:<сообщение>`): ответит ровно один экземпляр.
- Если ни одна сессия не на связи, через 90 с (или сразу, когда присутствия нет) отвечает
  серверный Claude — у него та же память.
- Пропущенное, пока ПК был выключен, новая сессия догоняет при старте.

Журнал приёма: `~/.cognitive-core/inbox.log`. Повторный запуск setup безопасен.

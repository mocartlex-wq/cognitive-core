---
name: 🐛 Bug Report
about: Сообщить о неожиданном поведении
title: "[BUG] "
labels: bug, needs-triage
assignees: ''
---

## Что произошло

Краткое описание бага в 1-2 предложениях.

## Шаги воспроизведения

1. Запустил '...'
2. Вызвал endpoint '...'
3. Получил '...'

## Ожидаемое поведение

Что должно было произойти.

## Фактическое поведение

Что произошло на самом деле. Скриншоты/логи если применимо.

## Окружение

- **Версия Cognitive Core**: (вывод `git log -1 --format=%H` или из CHANGELOG)
- **ОС**: (Windows 11 / Ubuntu 22.04 / macOS 14)
- **Docker**: (вывод `docker --version`)
- **AI клиент**: (Claude Desktop / Cursor / Cherry Studio / direct API)
- **Python (если запускаете нативно)**: (вывод `python --version`)

## Логи

```
вставьте сюда вывод:
docker logs cognitive_api --tail 100
```

Если применимо — также `docker logs cognitive_postgres --tail 50` или `mcp-server-cognitive-core.log` из Claude Desktop.

## Что вы пробовали

- [ ] Перезапуск стека: `docker compose restart`
- [ ] Прогон тестов: `docker exec cognitive_api python -m pytest tests/ -q`
- [ ] Проверка `/health`: `curl http://localhost:9001/health`
- [ ] Чтение [DEPLOY-SERVER.md](DEPLOY-SERVER.md) → Troubleshooting

## Дополнительный контекст

Любая информация которая может помочь.

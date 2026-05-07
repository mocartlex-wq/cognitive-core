# Contributing to Cognitive Core

Спасибо что хотите помочь. Этот документ — короткий путь от форка до merged PR.

## Quick start для контрибьютора

```bash
# 1. Fork на GitHub, потом
git clone https://github.com/YOUR_USERNAME/cognitive-core
cd cognitive-core
cp .env.example .env  # вставьте свой DEEPSEEK_API_KEY

# 2. Запуск local stack
docker compose up -d --build

# 3. Прогон тестов (114+ должно пройти)
docker exec cognitive_api python -m pytest tests/ -v

# 4. Создайте branch для своих изменений
git checkout -b feature/моё-улучшение
```

## Что мы ищем

| Тип контрибьюции | Приоритет |
|---|---|
| 🐛 Bug reports с воспроизведением | Очень высокий |
| 📝 Документация (typos, примеры, переводы) | Высокий |
| 🧪 Дополнительные тесты | Высокий |
| ⚡ Performance improvements (с бенчмарком) | Средний |
| ✨ Новые features (обсудить в issue сначала!) | Средний |
| 🌐 Новые языки промптов (сейчас 8) | Средний |
| 🎨 UI улучшения | Средний |

## Что мы **НЕ** примем без обсуждения

- Большие архитектурные изменения без RFC issue
- Новые зависимости (минимизируем external deps)
- Breaking changes API без миграции
- Code style violations (используем `ruff`)

## Структура проекта (где что лежит)

```
cognitive-core/
├── app/                      # FastAPI backend
│   ├── api/                  # REST endpoints (events, operative, memory, agents, ...)
│   ├── services/             # Business logic (consolidator, curator, embedder, ...)
│   ├── db/                   # Postgres, Redis, S3 adapters
│   ├── models/               # Pydantic schemas
│   ├── security/             # auth, sanitizer, audit
│   └── main.py               # FastAPI app + lifespan
├── mcp_server/               # MCP server для Claude Desktop / Cursor
├── cognitive-client/         # Python SDK
├── sandbox/                  # Web UI (vanilla HTML+CSS+JS)
├── scripts/                  # gen-secrets, install, backup, etc.
├── tests/                    # pytest, 114+ tests
├── nginx/                    # Production reverse proxy config
├── alembic/                  # DB migrations
└── docs/                     # README, DEPLOY, AGENT_GUIDE, etc.
```

## Code style

```bash
# Перед PR прогоните
ruff check app/ mcp_server/ tests/
ruff format app/ mcp_server/ tests/

# И тесты
docker exec cognitive_api python -m pytest tests/ -v
```

**Правила:**
- Type hints везде где возможно
- Docstrings для public functions
- Async-first для новых endpoints
- Russian/English mix в комментариях OK; user-facing text — обоих языках
- В .ps1 скриптах — **только English** (PowerShell 5.1 ломает кириллицу без BOM)

## Commit messages

Формат:
```
<type>: краткое описание (под 70 chars)

Подробное описание (если нужно):
- что изменилось
- почему
- ссылки на issue: #42
```

Types: `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `chore`, `security`.

## Pull Request checklist

- [ ] Тесты проходят: `docker exec cognitive_api python -m pytest tests/ -q`
- [ ] Lint чист: `ruff check`
- [ ] Новые публичные функции имеют docstrings
- [ ] CHANGELOG.md обновлён под `## Unreleased`
- [ ] Если изменили API: документация в `AGENT_GUIDE.md` обновлена
- [ ] Если изменили схему БД: Alembic миграция добавлена в `alembic/versions/`
- [ ] PR title в формате commit message

## Reporting bugs

Используйте template в `.github/ISSUE_TEMPLATE/bug_report.md`. Включите:
- Версия (git log -1 или CHANGELOG)
- ОС / Docker version
- Шаги воспроизведения
- Ожидаемое vs фактическое поведение
- Logs: `docker logs cognitive_api --tail 100`

## Security vulnerabilities

**НЕ открывайте публичный issue.** См. [SECURITY.md](SECURITY.md) для responsible disclosure.

## Discussion

- Для **больших** changes — сначала RFC issue для обсуждения
- Для **архитектурных** решений — упоминание `@maintainers` для двойного review
- Для **performance** improvements — приложите бенчмарки до/после

## Code of Conduct

См. [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). TL;DR — будьте вежливы, конструктивны, открыты к разному опыту.

## Release process (для maintainers)

```bash
# 1. Update CHANGELOG.md
# 2. Bump version in CHANGELOG header
# 3. Create signed tag
git tag -as v0.X.Y -m "Release v0.X.Y"

# 4. Push tag
git push origin v0.X.Y

# 5. Create GitHub Release с release notes
gh release create v0.X.Y --title "v0.X.Y" --notes-from-tag
```

## Большое спасибо!

Каждый contributor добавляется в `AUTHORS` файл. Для значительных вкладов — упоминание в release notes.

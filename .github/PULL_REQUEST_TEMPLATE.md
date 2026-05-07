# Pull Request

## Что меняется

Краткое описание в 1-3 предложения.

## Тип изменения

- [ ] 🐛 Bugfix
- [ ] ✨ New feature
- [ ] 📝 Documentation
- [ ] 🧪 Tests
- [ ] ♻️ Refactor (без изменения поведения)
- [ ] ⚡ Performance
- [ ] 🔒 Security
- [ ] 🔧 Build/CI

## Связанные issues

Closes #(номер) или Fixes #(номер).

## Чек-лист

- [ ] Тесты проходят: `docker exec cognitive_api python -m pytest tests/ -q`
- [ ] Lint: `ruff check app/ mcp_server/ tests/`
- [ ] Новые публичные функции имеют docstrings
- [ ] CHANGELOG.md обновлён (раздел `## Unreleased`)
- [ ] Документация обновлена (если применимо)
- [ ] Alembic миграция добавлена (если изменена схема)
- [ ] Самостоятельный review проведён

## Тестирование

Опишите как тестировали:
- Какие тесты добавлены
- Manual testing scenarios
- Production overlay тест (если затрагивает deployment)

## Скриншоты (если UI changes)

До и после — drag-and-drop в этот блок.

## Performance impact (если применимо)

Бенчмарки до и после, например:
```
Before: p95 = 145ms
After:  p95 = 89ms (-39%)
```

## Breaking changes

- [ ] Нет
- [ ] Да — описать миграцию: ...

## Дополнительный контекст

Что-то ещё что reviewer должен знать.

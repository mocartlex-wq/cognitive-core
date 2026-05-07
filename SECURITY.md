# Security Policy

## Threat Model

Cognitive Core разработан как self-hosted memory system. Threat model:

| Угроза | Защита |
|---|---|
| **Unauthorized API access** | X-API-Key per-agent в `AGENT_API_KEYS` (.env), длинные hex (32 байта) |
| **Brute force на API keys** | Rate-limit 100 req/sec per agent через Redis INCR |
| **SQL injection** | Все запросы через asyncpg parameterized (никогда string concat) |
| **JSON/XSS injection** | `sanitize_payload` валидирует и экранирует payload до записи в L1 |
| **Shell injection через MCP** | MCP-tools работают через httpx → REST API, не через shell |
| **Resource exhaustion** | MAX_PAYLOAD_SIZE=256KB, MAX_PAYLOAD_DEPTH=10, MAX_PAYLOAD_KEYS=500 |
| **DoS через токсичный JSON** | Pydantic strict validation + sanitizer reject deep nesting |
| **L5 audit tampering** | Audit-log пишется в Postgres, не редактируется через API |
| **Snapshot tampering** | L4 SHA-256 hash check при restore (`/memory/snapshots/{id}/verify`) |
| **MITM при передаче данных** | TLS termination через nginx (Let's Encrypt или self-signed) |
| **Container escape** | Docker isolation, нет privileged containers, ports внутри bridge |
| **Secrets в git** | `.env` в .gitignore, generators в `scripts/gen-secrets.sh` |
| **TLS keys в git** | `nginx/certs/*.{key,crt}` в .gitignore |

## Out of Scope

- **Атаки на Docker host** — предполагается что host безопасен (UFW, обновления)
- **Compromised LLM provider** — DeepSeek/OpenAI API trust model
- **User error в .env** — если admin раскрыл `.env` файл — secrets компрометированы

## Security Best Practices при деплое

1. **Generate strong secrets**: `bash scripts/gen-secrets.sh > .env`
2. **chmod 600 .env** — никто кроме owner не читает
3. **AGENT_API_KEYS** — длинные hex (32 байта = 64 chars)
4. **UFW firewall**: открыть только 22/80/443
5. **TLS обязательно**: Let's Encrypt для public домена
6. **Backups вне сервера**: rsync на DR-сервер
7. **Не пушить .env в git** (защищено .gitignore, но проверяйте)
8. **Регулярные обновления** базового Ubuntu образа: `sudo apt upgrade`
9. **Отключите MinIO console наружу**: в `docker-compose.prod.yml` — `ports: !reset []`
10. **Мониторинг auth_failure**: setup alert на рост `auth_failure` в L5 audit-log

## Reporting a Vulnerability

Если вы нашли уязвимость:

1. **НЕ открывайте публичный issue**
2. Отправьте детали на `security@cognitive-core.local` (замените на реальный email при публикации)
3. Включите:
   - Описание уязвимости
   - Шаги для воспроизведения
   - Версию (`git log -1` или CHANGELOG.md)
   - Минимальный proof-of-concept
4. Мы ответим в течение 7 дней
5. Безопасность раскрывается после фикса (responsible disclosure)

## Known Limitations (документированные)

| Ограничение | Workaround |
|---|---|
| Нет 2FA для агентов | API keys ротируются вручную, длинные хексы |
| Нет per-IP rate-limit | nginx добавляет `limit_req_zone $binary_remote_addr` (см. `nginx.conf`) |
| Нет шифрования at-rest для Postgres | Используйте LUKS на disk level или Postgres pgcrypto для sensitive payload |
| Нет аудита админских действий через CLI | docker exec действия не логируются — следите кто имеет SSH access |
| MinIO без TLS внутри docker network | Internal network isolation через bridge — наружу не выходит |

## Compliance

- **GDPR**: возможна self-hosted установка → данные не покидают вашу инфраструктуру
- **PII в payload**: рекомендация — не отправлять PII в `state_data` агента, использовать reference (например ID пользователя в external system)
- **Right to be forgotten**: SQL `DELETE FROM l1_raw_events WHERE source_agent = ?` + DELETE из l3 + restore из L4

## Версии и поддержка

| Версия | Поддержка |
|---|---|
| 0.5.x | Текущая, активная |
| 0.4.x | Security fixes до 2026-08 |
| < 0.4 | Не поддерживается, обновляйтесь |

## Дополнительно

- См. [`DEPLOY-SERVER.md`](DEPLOY-SERVER.md) — Security checklist при деплое
- См. [`CHANGELOG.md`](CHANGELOG.md) — security-related changes отмечаются

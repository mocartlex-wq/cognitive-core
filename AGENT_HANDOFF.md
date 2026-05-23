# Agent Handoff Protocol — Cognitive Core ecosystem

> Стандартная процедура подключения нового AI-агента (Claude Code на другой машине, Cursor, Cherry Studio, custom) к экосистеме Cognitive Core. Сформирована на опыте 2026-05-07 (подключение AI-CRM агента).

## TL;DR — checklist для координирующего агента

При подключении нового агента надо выдать **7 артефактов**:

1. SSH-доступ под `salex@192.168.0.118` (LAN) / `salex@10.66.66.1` (WG) — добавить новый pubkey в `~/.ssh/authorized_keys`
2. Каталог `/opt/<project>/` (rwxr-xr-x, owner `salex`)
3. Bare-repo `/home/salex/<project>.git` для git-push deploy
4. Postgres БД `<project>` + пользователь `<project>_user` со сгенерированным паролем
5. MinIO bucket `<project>` + access key/secret (отдельный, не общий с cognitive-core)
6. Файл `/home/salex/<project>-handoff.env` (chmod 600) — все credentials в одном месте, читается агентом при первом подключении (не передаётся в чат!)
7. Ссылка на `AGENT_RULES.md` (правила работы) + `AGENT_OPERATIONS.md` (инфра-справка)

**Порядок:** ресурсы (2-6) можно создать сразу когда известно имя проекта. Pubkey (1) — только когда агент его прислал.

---

## 1. Конвенции именования

| Артефакт | Шаблон | Пример |
|---|---|---|
| Project name | `kebab-case`, короткий, английский | `ai-crm`, `analytics`, `bot-core` |
| Working dir | `/opt/<project>/` | `/opt/ai-crm/` |
| Bare-repo | `/home/salex/<project>.git` | `/home/salex/ai-crm.git` |
| Postgres DB | `<project>` (с дефисом, в кавычках в SQL) | `ai-crm` |
| Postgres user | `<project>_user` | `ai_crm_user` (Postgres NAMEDATALEN — 63, дефисы можно) |
| MinIO bucket | `<project>` | `ai-crm` |
| MinIO access key | `<project>-agent` | `ai-crm-agent` |
| Handoff env | `/home/salex/<project>-handoff.env` | `/home/salex/ai-crm-handoff.env` |
| Subdomain (если нужен публичный endpoint) | `<project>.ии-память.рф` | `crm.ии-память.рф` |

---

## 2. Что должен прислать новый агент

```
1. Имя проекта (kebab-case)
2. Публичный SSH-ключ (одна строка, начинается с ssh-ed25519 или ssh-rsa)
3. (опционально) Список доп-ресурсов:
   - Дополнительная БД / отдельная Postgres schema
   - Дополнительный bucket для бинарных данных (multimodal, etc)
   - Поддомен для публичного endpoint
   - Открытые порты (если выходит за пределы общего nginx)
```

Минимум — пункты 1 и 2. Остальное добавляется по ходу работы.

---

## 3. Что выдаёт координирующий агент

Шаблон `<project>-handoff.env`:

```bash
# AI-CRM — handoff credentials
# Создан 2026-05-07 координирующим агентом cognitive-core

# Postgres
PG_HOST=postgres            # internal docker DNS (внутри cognitive-core_default network)
PG_PORT=5432
PG_DATABASE=ai-crm
PG_USER=ai_crm_user
PG_PASSWORD=<generated-32-chars>

# MinIO / S3
S3_ENDPOINT=http://minio:9000
S3_BUCKET=ai-crm
S3_ACCESS_KEY=ai-crm-agent
S3_SECRET_KEY=<generated-40-chars>
S3_REGION=us-east-1

# Cognitive Core MCP (для inter-agent collaboration)
COGNITIVE_CORE_URL=https://mcp.me-ai.ru
COGNITIVE_CORE_API_KEY=<per-agent-key>

# Server topology
WORKING_DIR=/opt/ai-crm
BARE_REPO=/home/salex/ai-crm.git
```

Файл — `chmod 600`, читается только salex (т.е. через ssh-сессию агента).

---

## 4. Что нового агент должен прочитать первым делом

В порядке приоритета:

1. **`AGENT_RULES.md`** — общие правила (язык, DS-second-voice, simple-first, dev/stable, no-direct-server-edit, и т.д.)
2. **`AGENT_OPERATIONS.md` (cognitive-core repo)** — инфра-справка по серверу (порты, контейнеры, certbot, UFW, гражbles)
3. **`FAST_MEMORY.md`** — как использовать быструю память для realtime координации
4. **Свой `<project>-handoff.env`** — где взять credentials к ресурсам

---

## 5. Первый push нового агента

```bash
# на машине агента
ssh-keygen -t ed25519 -f ~/.ssh/cogserver_deploy -N "" -C "<project>@<machine>"
cat ~/.ssh/cogserver_deploy.pub        # → отправить координатору

# подождать подтверждения "ключ добавлен, ресурсы готовы"

ssh -i ~/.ssh/cogserver_deploy salex@192.168.0.118 'cat /home/salex/<project>-handoff.env'
# скопировать в локальный .env у себя

# инициализировать репо локально
cd <local-project-dir>
git init -b main
git add .
git commit -m "Initial commit: <project> v0.1"
git remote add server salex@192.168.0.118:/home/salex/<project>.git
GIT_SSH_COMMAND="ssh -i ~/.ssh/cogserver_deploy" git push -u server main

# скопировать паттерн auto-deploy у cognitive-core
ssh -i ~/.ssh/cogserver_deploy salex@192.168.0.118 \
  "git clone /home/salex/cognitive-core.git /tmp/cog-template && \
   cp -r /tmp/cog-template/scripts/auto-deploy.sh /tmp/cog-template/scripts/conditional_reload.sh \
         /tmp/cog-template/deploy /opt/<project>/scripts-template/"

# адаптировать под свой проект (compose-файлы, порты), потом установить таймер:
ssh -i ~/.ssh/cogserver_deploy salex@192.168.0.118 \
  "cd /opt/<project> && bash deploy/install-auto-deploy.sh"
```

---

## 6. Регистрация в Cognitive Core (когда v0.5.5 готов)

```bash
# одной командой, при старте агента:
curl -X POST https://mcp.me-ai.ru/agents/register \
  -H "X-API-Key: <per-agent-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "ai-crm-server-v1",
    "project": "ai-crm",
    "machine": "<hostname>",
    "capabilities": ["python", "fastapi", "postgres"],
    "version": "0.1.0"
  }'
```

После этого:
- Координирующий агент видит его через `GET /agents/online?project=ai-crm`
- Direct messages идут в `agent_inbox` domain
- Heartbeat нужен раз в 30 сек

До v0.5.5 — агенты обмениваются через L1 events (`POST /events` с `domain=agent-handoff`).

---

## 7. Антипаттерны handoff

- **Передача private SSH key** в чате — даёт полный shell-доступ. Только public key.
- **Передача production Postgres super-user creds** новому агенту — каждому проекту свой ограниченный user.
- **Создание агента с sudo NOPASSWD на cognitive-core операции** — изоляция через файловые права на `/opt/<project>/`.
- **Хранение creds в git** — только в `.env.example` без секретов; реальный `.env` через handoff-файл и в `.gitignore`.
- **Прямые правки на сервере** — через git push, не через ssh+vim.
- **«Одна БД на всех» с table-prefix** — каждому проекту своя БД, изоляция.

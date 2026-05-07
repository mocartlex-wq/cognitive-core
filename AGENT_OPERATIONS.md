# Cognitive Core — Agent Operations Guide

> Этот документ — для AI-агентов (Claude Desktop, Claude Code, Cursor, других) и людей, работающих с системой Cognitive Core. Содержит точные адреса, ключи, топологию и типовые операции.
>
> **Обновлять по мере изменений.** В первую очередь читать новому агенту в начале сессии.

---

## TL;DR — что это и где

**Cognitive Core** — 5-слойная самохостимая память для AI-агентов. Сервер — Ubuntu 24, г. Пенза, 32 GB RAM. С 2026-05-06 доступен **публично через HTTPS без VPN**.

| Точка входа | Когда использовать |
|---|---|
| **MCP**: `https://mcp.ии-память.рф/mcp/sse` | Подключение AI-агентов через Model Context Protocol |
| **API**: `https://mcp.ии-память.рф/` | REST-обращения к слоям L1-L4, дашборд, песочница |
| **SSH**: `ssh salex@10.66.66.1` | Админ-доступ к серверу (через WireGuard) |

---

## 🌐 Публичные endpoint'ы

База: `https://mcp.ии-память.рф` (Punycode: `mcp.xn----8sbwawqx4fza.xn--p1ai`)

### Открытые без аутентификации
| Путь | Что |
|---|---|
| `GET /health` | Состояние системы (healthy, layers, embedding, llm) |
| `GET /` | Главная страница (Glass UI лендинг) |
| `GET /ui` | Дашборд состояния памяти |
| `GET /sandbox` | API-песочница (HTML) |
| `GET /metrics` | Prometheus-метрики (доступ только из 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16) |

### Требуют `X-API-Key` header
| Путь | Что |
|---|---|
| `POST /events` | Запись L1-события |
| `POST /operative/query` | KNN-поиск по L3 + создание OP-сессии |
| `POST /memory/consolidate/daily\|weekly` | LLM-консолидация |
| `POST /memory/audit/monthly` | L3-аудит |
| `POST /memory/snapshot/full\|delta` | Создание снапшотов в L4 |
| `GET/POST /tools/*` | Реестр инструментов |
| `GET/POST /agents/*` | Состояния агентов |
| `POST /demo/run` | Streaming-демо (rate-limit 2 req/min) |

### MCP transport (для AI-клиентов)
| Путь | Назначение |
|---|---|
| `GET /mcp/sse` | Server-Sent Events init-канал |
| `POST /messages/?session_id=...` | JSON-RPC сообщения |

Долгий poll, `proxy_read_timeout 24h`, всё буферизирование выключено.

---

## 🔐 API-ключи

Ключи **не хранить в репо**. Все секреты живут в `/opt/cognitive-core/.env` на сервере.

### Известные ключи и их назначение
| Ключ | Назначение | Где использовать |
|---|---|---|
| `fc9a6aa56e7b43a788acbf3e8fb46bd1bf264aaacc876790bb4fe6ac88a65244` | Default agent key для MCP-клиентов | Header `X-API-Key:` при подключении к `/mcp/sse` |
| `key-design-001` | agent_designer (legacy demo) | Старый ключ из CLAUDE.md, для тестов |
| `key-dev-001` | agent_developer (legacy demo) | То же |
| `DEEPSEEK_API_KEY` | LLM-провайдер | В `.env`, не передавать наружу |
| `POSTGRES_PASSWORD` / `S3_ACCESS_KEY` / `S3_SECRET_KEY` | Внутренние пароли БД и MinIO | В `.env`, не торчат наружу через nginx |

**Ротация ключей:** изменение в `.env` + restart api контейнера. По плану — будет manifest tool с ключевой стратегией (см. `AGENT_GUIDE.md` раздел про architecture rules).

---

## 🖥️ Сервер — топология и доступ

### Hardware
- **Hostname**: `salex`
- **OS**: Ubuntu 24.04.4 LTS
- **CPU**: 4 cores
- **RAM**: 32 GB DDR4
- **Disk**: 98 GB total, ~76 GB free

### Сеть
- **WAN IP** (статический белый): `94.181.169.239`
- **LAN IP**: `192.168.0.118/24`
- **WG IP** (`wg0`): `10.66.66.1/24`, peer-server, порт `51820/udp`
- **ISP**: Дом.ру Бизнес (ER-Telecom), г. Пенза
- **Подключение**: PPPoE
- **Роутер**: TP-LINK TL-WR842N, `192.168.0.1:8080` (админка перенесена с 80)

### Port-forwards (TP-LINK Virtual Servers)
| Внеш. порт | Протокол | На какой LAN-IP | Состояние |
|---|---|---|---|
| 80 | TCP | 192.168.0.118 | Включён (для Let's Encrypt + HTTP→HTTPS redirect) |
| 443 | TCP | 192.168.0.118 | Включён (nginx HTTPS) |
| 51820 | UDP | (router сам) | WireGuard server |

### UFW на сервере (server-side firewall)
```
22/tcp        ALLOW IN  Anywhere       # SSH
80/tcp        ALLOW IN  Anywhere       # http for letsencrypt
443/tcp       ALLOW IN  Anywhere       # https for nginx
9001/tcp      ALLOW IN  Anywhere       # CogAPI direct (legacy, рассмотреть закрытие)
9002/tcp      ALLOW IN  Anywhere       # MinIO console
51820/udp     ALLOW IN  Anywhere       # WireGuard
5432          ALLOW IN  192.168.0.0/24 # PG для LAN
6379          ALLOW IN  192.168.0.0/24 # Redis для LAN
```

⚠️ **Если порт снаружи timeout** — первое что проверить: `sudo ufw status numbered`. До 2026-05-06 это была причина диагностики на 3+ часа.

### SSH-доступ
| Путь | Команда | Когда |
|---|---|---|
| Через WG | `ssh -i ~/.ssh/cogcore_lan salex@10.66.66.1` | Удалённый доступ, любая точка мира |
| Через LAN | `ssh salex@192.168.0.118` | Только когда физически в офисе |
| Через WAN+22 | `ssh salex@94.181.169.239` | Если 22 будет открыт в UFW и port-forward (сейчас НЕ настроено) |

SSH-ключ для агента: `~/.ssh/cogcore_lan` на машине пользователя.

---

## 🐳 Docker-стек на сервере

Каталог: `/opt/cognitive-core/`. Compose-файлы:
- `docker-compose.yml` — базовый dev-стек
- `docker-compose.prod.yml` — production-overlay (nginx, resource-limits, log-rotation, backup)

### Запущенные контейнеры (production)
| Имя | Образ | Порты | Назначение |
|---|---|---|---|
| `cognitive_nginx` | nginx:1.27-alpine | 80, 443 наружу | TLS termination, reverse-proxy, rate-limit |
| `cognitive_api` | cognitive-core-api | 9001 → 8000 | FastAPI: REST, lifespan, scheduler |
| `cognitive_mcp` | cognitive-core-api (entry: mcp_server) | 8765 (только LAN/WG/internal) | Model Context Protocol SSE |
| `cognitive_postgres` | postgres:16-alpine | 5432 (LAN) | Слои L1-L5, agent_states |
| `cognitive_redis` | redis:7-alpine (Stack) | 6379 + 8001 RedisInsight | Кэш, сессии OP, KNN-индекс |
| `cognitive_minio` | minio/minio | 9000+9001 internal | S3 для L4-снапшотов |
| `cognitive_backup` | alpine + cron | none | Cron-бэкапы pg+minio в `/opt/cognitive-core/backups/` |

### Сетевая топология контейнеров
- Все в одном docker network `cognitive-core_default` (172.18.0.0/16)
- Между собой ходят по DNS-именам: `api`, `mcp`, `postgres`, `redis`, `minio`, `nginx`
- ⚠️ В `nginx.conf` для `/mcp/` использовать `proxy_pass http://mcp:8765/;` — **не** `api:8765`!

### Lifecycle commands
```bash
# Запуск всего production стека
cd /opt/cognitive-core
sudo docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d

# Только nginx (после правки nginx.conf)
sudo docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d nginx

# Reload nginx без рестарта (после правки конфига)
sudo docker exec cognitive_nginx nginx -s reload

# Проверка состояния
sudo docker ps --filter name=cognitive_
curl -s https://mcp.ии-память.рф/health | jq

# Логи
sudo docker logs cognitive_nginx --tail 50
sudo docker logs cognitive_api --tail 50
sudo docker logs cognitive_mcp --tail 50

# Тесты
sudo docker exec cognitive_api python -m pytest tests/ -v
```

---

## 🔒 TLS / Let's Encrypt

- Cert на `mcp.xn----8sbwawqx4fza.xn--p1ai`, выдан 2026-05-06, истекает **2026-08-04**
- Локация: `/etc/letsencrypt/live/mcp.xn----8sbwawqx4fza.xn--p1ai/`
- Симлинки в `/opt/cognitive-core/nginx/certs/server.crt|key`
- Auto-renewal: `systemctl status certbot.timer` (запускается каждый день, обновит за 30 дней до истечения)
- При renewal nginx **не** автоматически переподхватит — нужен `docker exec cognitive_nginx nginx -s reload` post-hook (см. TODO ниже)

### Получить новый/расширенный cert
```bash
sudo bash /opt/cognitive-core/scripts/setup-tls.sh letsencrypt <punycode-domain> <email>
```
Перед запуском: остановить любой listener на 80 (`sudo systemctl stop nginx` если есть, и **остановить cognitive_nginx** если он висит на 80, либо использовать `--webroot` challenge).

---

## 🧠 DNS

Регистратор: **uKit** (через REG.RU/ER-Telecom). Управление DNS — там же.

### Действующие записи (зона `ии-память.рф`)
```
@      SOA   ns1.ukit.com  (служебная)
@      NS    ns1.ukit.com
@      NS    ns2.ukit.com
@      NS    ns3.ukit.com
@      A     185.129.100.127     # uKit-сайт (заглушка/landing)
www    A     185.129.100.127     # туда же
mcp    A     94.181.169.239      # ← НАШ СЕРВЕР
```

### Ограничения uKit
- DNS-редактор появляется **только** после прикрепления домена к одному из uKit-сайтов (любой пустой шаблон подойдёт)
- NS-записи менять может только техподдержка uKit (через тикет)
- Для добавления A/CNAME — личный кабинет uKit, раздел «Редактирование записей»

---

## 🚨 Известные грабли (lessons learned)

### 1. UFW блокирует всё кроме SSH по умолчанию
**Симптом:** Внешний `curl` к 80/443/любому порту — `Connection timed out` или `Connection reset`. При этом локальный `curl localhost:N` работает.

**Решение:** `sudo ufw allow N/tcp` на сервере.

**Памятка:** при любой проблеме «снаружи не достучаться» — **сначала** `sudo ufw status numbered`. Сэкономит часы.

### 2. Системный nginx (apt) против docker-nginx
`install-server.sh` ставит **системный** nginx на 80, docker-prod-overlay тоже хочет 80 — конфликт. После установки системный nginx **отключить**:
```
sudo systemctl stop nginx
sudo systemctl disable nginx
```

### 3. nginx.conf для MCP-роута
В репо стоит `proxy_pass http://api:8765/;` — это **legacy** (когда MCP запускался внутри api-контейнера). Текущая архитектура: MCP в отдельном контейнере `cognitive_mcp` (alias `mcp` в docker network). Правильно:
```
location /mcp/ {
    proxy_pass http://mcp:8765/;
    ...
}
```

**Подвох с FastMCP 2.x SSE.** SSE init-кадр `mcp.sse_app()` отдаёт *относительный* path:
```
event: endpoint
data: /messages/?session_id=<id>
```
Без переписывания клиент склеит с корнем домена и попадёт в `location /` (cognitive_api) → 404 на handshake. Нужны **отдельные роуты** `location = /mcp/sse` (с `sub_filter 'data: /messages/' 'data: /mcp/messages/'`) и `location /mcp/messages/` (proxy_pass на mcp:8765/messages/). Применено в `nginx/nginx.conf` 2026-05-06.

### 8. WireGuard endpoint: WAN, не LAN
Импортированный туннель `cognitive-server-lan` имеет в конфиге peer `Endpoint = 192.168.0.118:51820` — это LAN-IP офисного роутера. Из дома handshake не пройдёт никогда, потому что endpoint — приватный адрес. **Поправить на WAN:** `Endpoint = 94.181.169.239:51820` (через GUI: «Изменить» → save). Port-forward 51820/udp на роутере открыт, на сервере UFW тоже разрешает — извне работает. Заметка: WireGuard GUI на Windows показывает список туннелей **только если запущен от админа** (конфиги в `C:\Program Files\WireGuard\Data\Configurations\` под protected ACL).

### 4. TP-LINK веб-админка на 80 vs port-forward
Если админка роутера на 80 — port-forward 80 не сохраняется (TP-LINK защищает от конфликта). Решение: System Tools → Remote Management → Web Management Port = `8080`. Reboot router. Теперь админка по `http://192.168.0.1:8080`, а 80 свободен для port-forward.

### 5. Rate-limit от Дом.ру / антиспам — миф
ISP **не блокирует** входящие 80/443 на этом тарифе/IP (подтверждено саппортом и тестами). Любые проблемы доступа — **ищи у себя** (UFW, роутер, nginx, контейнеры).

### 6. uKit DNS появляется только после прикрепления к сайту
Если в uKit на странице домена видно только NS-записи — нужно создать любой uKit-сайт (бесплатный шаблон-заглушка) и привязать к нему этот домен. Тогда появится «Редактирование записей» с управлением A/CNAME/TXT.

### 7. IDN-домены и Punycode
В JSON/YAML-конфигах (например `claude_desktop_config.json`) использовать **Punycode** форму: `mcp.xn----8sbwawqx4fza.xn--p1ai`. В человекочитаемых местах (README, чаты, доки) — `mcp.ии-память.рф`. Большинство современных HTTP-клиентов оба понимают, но IDN в JSON — потенциальный источник кодировок-проблем.

---

## 🎯 Onboarding-протокол для нового AI-агента

В первом ходе сессии работающей с Cognitive Core, **последовательность действий**:

1. **Прочитать этот файл** (`AGENT_OPERATIONS.md`) если работаешь с проектом впервые — понять топологию.
2. **Прочитать `AGENT_GUIDE.md`** в том же каталоге — там агентские конвенции по доменам, типам записей, sanitizer'у.
3. **Дёрнуть** `mcp__cognitive-core__cognitive_health` через MCP. Если не подключён — клиент не настроен на новый endpoint, см. секцию «Подключение MCP-клиента».
4. **Узнать список доменов** через `cognitive_domains` — увидеть масштаб.
5. **Если возобновляешь работу** — `cognitive_continue` для восстановления state.

При нетривиальных решениях (архитектура, выбор стека, API-дизайн) — **консультироваться с DeepSeek** через DeepSeek API (ключ в `.env`). Это правило проекта (см. memory `feedback_deepseek_second_voice`).

### Подключение MCP-клиента
Текущий `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "cognitive-core": {
      "command": "cmd",
      "args": [
        "/c", "npx", "-y", "mcp-remote@latest",
        "https://mcp.xn----8sbwawqx4fza.xn--p1ai/mcp/sse",
        "--header",
        "X-API-Key:fc9a6aa56e7b43a788acbf3e8fb46bd1bf264aaacc876790bb4fe6ac88a65244"
      ]
    }
  }
}
```

Для **Cursor** — аналогично, использует тот же mcp-remote.
Для **Claude Code** — может работать через stdio bridge, либо подключаться напрямую к SSE.

После правки конфига — **перезапустить клиент** (Claude Desktop / Code).

---

## 📦 Auto-deploy pipeline

С 2026-05-07 сервер тянет изменения **сам** через systemd-timer + `git fetch`. Никаких ручных `scp` или `docker exec` для обычных правок.

**С 2026-05-07T13:00 UTC основной remote — GitHub** (`git@github.com:mocartlex-wq/cognitive-core.git`, приватный, deploy-key read-only). Push в GitHub из любой точки мира → через 60 сек на сервере. WG/LAN не нужны для деплоя обычных изменений.

`/home/salex/cognitive-core.git` остаётся как secondary mirror на случай отказа GitHub.

### Как это работает

1. **Локально** (или с любой машины — github.com web-UI, github.dev, мобильник): `git push origin main`
2. **На сервере** `cognitive-deploy.timer` каждые 60 сек запускает `cognitive-deploy.service` → `scripts/auto-deploy.sh`
3. `auto-deploy.sh` делает `git fetch origin main`, сравнивает HEAD; если изменилось — `git pull --ff-only` и зовёт `conditional_reload.sh`
4. `conditional_reload.sh` смотрит `git diff --name-only` и решает что трогать:

| Изменилось | Что делается |
|---|---|
| `nginx/*` | `docker exec cognitive_nginx nginx -t && nginx -s reload` |
| `app/*`, `alembic/*`, `requirements*.txt`, `pyproject.toml`, `Dockerfile` | rebuild **api** + **mcp** (общий код) |
| `mcp_server/*` | rebuild только **mcp** |
| `docker-compose*.yml`, `.env*` | full `compose up -d --build` |
| `scripts/auto-deploy.sh`, `conditional_reload.sh`, `deploy/*` | `systemctl daemon-reload` + restart timer |
| `*.md`, `docs/*`, прочие scripts | ничего |

### Установка (один раз)

```bash
# на сервере (требует SSH через WG)
cd /opt/cognitive-core
git remote add origin git@github.com:<owner>/cognitive-core.git
git fetch origin
git reset --hard origin/main
bash deploy/install-auto-deploy.sh
```

### Наблюдение

```bash
journalctl -u cognitive-deploy -n 50 -f         # live deploy log
systemctl status cognitive-deploy.timer         # next run time
```

### Когда WG всё-таки нужен

- Диагностика по логам (`docker logs cognitive_api`, `journalctl`)
- Аварийный рестарт сервиса
- Изменение конфигов вне репо (`/etc/letsencrypt/`, `.env` в `/opt/cognitive-core/`)
- Установка новых system-deps

Для рутинных правок кода/nginx/compose — **только `git push`**, WG не нужен.

---

## 📋 TODO / нерешённое

### ✅ Сделано (2026-05-06, late session)
- [x] **Запатчен `nginx.conf` в репо**: `/mcp/` теперь идёт на `mcp:8765` (было `api:8765`)
- [x] **`server_name mcp.xn----8sbwawqx4fza.xn--p1ai mcp.ии-память.рф;`** — конкретный, default-сервер отбивает чужие SNI через `ssl_reject_handshake on;`
- [x] **certbot post-renewal hook** в `/etc/letsencrypt/renewal-hooks/post/reload-cognitive-nginx.sh` — копирует cert в nginx/certs и перезагружает контейнер
- [x] **UFW** для `9001/9002` закрыт снаружи, оставлен **только** для WG (10.66.66.0/24) и LAN (192.168.0.0/24). Direct API теперь только через nginx → :443
- [x] **`cognitive_agent_manifest` MCP-tool** реализован (см. ниже)
- [x] **MCP `/messages/` 404 fix** — `nginx.conf` дополнен `location = /mcp/sse` с `sub_filter` (переписывает relative path в SSE-фрейме) и `location /mcp/messages/` (proxy на `mcp:8765/messages/`). До фикса claude.ai/code и др. SSE-клиенты падали с "Server disconnected" на `initialize` handshake.
- [x] **Auto-deploy pipeline**: `scripts/auto-deploy.sh` + `scripts/conditional_reload.sh` + `deploy/cognitive-deploy.{service,timer}` + `deploy/install-auto-deploy.sh`. Сервер тянет изменения сам через systemd-timer + `git fetch`, conditional reload по diff'у. Подробности — раздел «Auto-deploy pipeline» выше.

### Осталось
- [ ] Middleware enforcement: первый вызов в сессии → manifest, иначе 412 (сейчас manifest опциональный, soft-rule)
- [ ] Replication L1 server → local через NATS JetStream (когда понадобится hot-spare)
- [ ] Обновить hardcoded `version: "0.2.0"` в `app/main.py` (стейл с момента релиза v0.2 → нужно `0.5.0`+)
- [ ] Лендинг на главном `ии-память.рф` (сейчас пустой uKit-сайт)
- [ ] Закоммитить эти изменения в git и запушить (репо локальный, синхронизирован вручную)

## 🆕 cognitive_agent_manifest — onboarding-tool

**Появился 2026-05-06.** Главное приобретение для self-discovery агентов.

Вызов: `mcp__cognitive-core__cognitive_agent_manifest()` — без аргументов. Возвращает:
```
{
  schema_version,        # "1.0" — для совместимости при будущих изменениях
  served_at,             # ISO timestamp
  topology: {            # где живёт API/MCP
    primary_endpoint_public, primary_endpoint_punycode, mcp_sse_path,
    current_api_url, transport_modes
  },
  rules: {               # operational rules для агентов
    on_session_start, before_complex_decision, after_significant_action,
    save_state_every, before_session_end, deepseek_consultation
  },
  agent_identity: {      # кто я и что мне доступно
    agent_name, agent_key_present, key_required_for, key_NOT_required_for
  },
  system: {              # live-state из /health
    healthy, version, uptime_seconds, services, embedding_provider, llm
  },
  layers: {              # размеры слоёв L1-L4
    l1, l2, l3_knowledge, l3_tools, l4
  },
  domains_top: [...],    # топ-10 доменов по активности
  last_checkpoint: {     # последний сохранённый state агента
    exists, current_task, last_checkpoint_at, active_session_ids_count
  },
  hints: {               # подсказки по частым задачам
    find_relevant_knowledge, list_what_we_know, check_what_tools_exist,
    quick_record_decision, operational_rules
  }
}
```

**Onboarding-протокол** (ОБНОВЛЁННЫЙ — теперь 2 шага):
1. `cognitive_agent_manifest()` — узнать кто я, где система, какие правила
2. `cognitive_continue()` — восстановить state с последнего checkpoint

---

## 📚 Ссылки на другие документы

- `AGENT_GUIDE.md` — агентские конвенции (типы записей, sanitizer, мульти-LLM fallback)
- `CLAUDE.md` — инструкции для Claude Code (build/run, архитектура, тесты)
- `DEPLOY-SERVER.md` — runbook деплоя на чистый сервер (install-server.sh)
- `roadmap.md` — план развития
- `CHANGELOG.md` — история версий
- `SECURITY.md` — модель угроз и mitigations

---

**Последнее обновление**: 2026-05-06 (поднят публичный HTTPS-endpoint mcp.ии-память.рф)

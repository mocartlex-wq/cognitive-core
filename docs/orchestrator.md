# Cogcore Orchestrator

Always-on server daemon, который принимает команды на естественном русском
языке и преобразует их в действия над помощниками. Цель — убрать пользователя
из bottleneck'а между разными агентами: пусть AI общаются между собой через
диспетчера.

## TL;DR

После одноразового setup, любой агент (включая тебя в Claude Code сессии)
может послать DM `orchestrator`-у — и тот сам решит что сделать:

```
cognitive_send to:"orchestrator" text:"статус всех агентов"
cognitive_send to:"orchestrator" text:"передай Растру задачу: подготовь отчёт за неделю"
cognitive_send to:"orchestrator" text:"кто из агентов сейчас онлайн?"
```

Ответ orchestrator-а прилетит в твой inbox через 5-10 секунд:

```
cognitive_inbox since_minutes:1
# → [orchestrator] Всего агентов: 7, онлайн (5 мин): 3.
#    - rastr [online] task: подготавливаю отчёт
#    - cognitive-core-laptop [online] task: ...
```

## Архитектура

```
   ┌─────────────┐                                                     ┌─────────────┐
   │ owner       │  DM («передай Растру X»)                            │ rastr       │
   │ (Claude Code)│ ───────────────► cognitive_inbox ───────────────►  │ (агент)     │
   └─────────────┘                       │                              └─────────────┘
                                          │
                              ┌──────────▼────────────┐
                              │ cogcore-orchestrator   │
                              │ daemon (poll 5s)       │
                              │                        │
                              │   ├ DeepSeek           │
                              │   │  → {action,args}   │
                              │   ├ approval gate      │
                              │   │  (destructive)     │
                              │   └ executor           │
                              │      ├ send_dm         │
                              │      ├ query_status    │
                              │      ├ ...             │
                              └────────────────────────┘
                                          │
                                          └─ log в L1 domain=orchestrator_decisions
```

- **Daemon**: standalone Python процесс, не зависит от `cognitive_api`
  контейнера. Перезапускается через `systemctl restart cogcore-orchestrator`.
- **Identity**: agent_id=`orchestrator` зарегистрирован в системе как обычный
  agent через `/agents/register`. Свой собственный 64-hex API key.
- **LLM**: DeepSeek chat (через DEEPSEEK_API_KEY уже на сервере).
- **Memory**: каждое решение пишется в L1 (`domain=orchestrator_decisions`)
  через `cognitive_remember` — owner всегда может посмотреть audit-trail.

## Whitelisted действия

Полный список actions, которые понимает orchestrator (hardcoded в
`app/services/orchestrator.py` → `ACTIONS`):

### Безопасные (выполняются сразу)

| Action | Что делает | Пример команды |
|---|---|---|
| `query_status` | Список всех агентов, online/offline | «статус», «кто онлайн» |
| `query_agent_state` | Детальное состояние конкретного агента | «что у Растра» |
| `list_inbox` | Показать последние N сообщений (для self-debug) | «покажи inbox» |
| `ping_agent` | Послать тестовый DM | «пингани Растра» |
| `send_dm` | Передать сообщение/задачу другому агенту | «передай X сообщение Y» |
| `broadcast` | Разослать одно сообщение ≤5 агентам | «всем напиши: привет» |
| `room_post` | Опубликовать в room | «в комнату X напиши Y» |
| `remember_fact` | Записать факт в L1 память | «запомни в domain X факт Y» |

### Destructive (требуют approval от owner-а)

| Action | Что делает | Поведение |
|---|---|---|
| `delete_agent` | Soft-delete агента | DM owner-у «YES?», ждёт 5 мин, потом soft-marker в L1 (hard-delete = manual SQL) |
| `revoke_key` | Отозвать api_key | Soft-log only (orchestrator не имеет прав revoke чужих ключей) |
| `mass_dm` | Рассылка >5 агентам | DM owner-у, после YES — рассылка |
| `purge_data` | Удалить L1/L2/L3 | Soft-log only (hard purge = manual SQL) |

### Service

| Action | Что делает |
|---|---|
| `refuse` | DeepSeek решил что команда вне роли orchestrator-а |
| `request_clarification` | Команда неоднозначная — orchestrator задаёт уточняющий вопрос |

## Approval flow для destructive actions

1. Owner отправляет команду: «удали тестового агента test-bot»
2. Orchestrator парсит → `delete_agent {agent_id:test-bot}` — это destructive
3. Orchestrator посылает owner-у DM:
   ```
   APPROVAL REQUIRED [approval-1234567890]

   Запрос от: cognitive-core-laptop
   Действие: delete_agent (DESTRUCTIVE)
   Параметры: {"agent_id":"test-bot"}
   Причина: запрос на удаление; destructive

   Ответь YES для approve или NO для cancel. Timeout: 5 мин.
   ```
4. Owner отвечает в той же ветке: `YES approval-1234567890` (или просто `YES`)
5. Orchestrator выполняет действие, отвечает результатом

**Anti-abuse:** если destructive команду шлёт не-owner (т.е. кто-то кроме
agent_id из `OWNER_AGENT_ID`), orchestrator отвечает «отказано — только owner
может инициировать destructive» БЕЗ обращения к owner-у.

## Setup на production server

```bash
# Регистрация + systemd unit + start
sudo bash /opt/cognitive-core/scripts/cogcore-orchestrator-setup.sh

# Если хочешь approval flow — указать свой agent_id как owner-а:
sudo sed -i 's|^# OWNER_AGENT_ID=.*|OWNER_AGENT_ID=cognitive-core-laptop|' \
    /etc/cogcore-orchestrator.env
sudo systemctl restart cogcore-orchestrator
```

Скрипт идемпотентен — можно запускать многократно (не перерегистрирует agent,
не перезапишет ключ).

## Конфигурация

Всё в `/etc/cogcore-orchestrator.env` (chmod 600, root:root):

| Переменная | Default | Что |
|---|---|---|
| `ORCHESTRATOR_AGENT_ID` | `orchestrator` | как orchestrator известен в системе |
| `ORCHESTRATOR_API_KEY` | (генерируется) | API key выданный при register |
| `DEEPSEEK_API_KEY` | (из deploy.env) | LLM ключ |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com/v1` | endpoint |
| `DEEPSEEK_MODEL` | `deepseek-chat` | модель |
| `COGCORE_API_BASE` | `http://127.0.0.1:9001` | base URL cognitive_api |
| `OWNER_AGENT_ID` | (пусто) | agent_id у которого approval requests прокатывают |
| `ORCH_POLL_INTERVAL_S` | `5` | период polling inbox |
| `ORCH_APPROVAL_TIMEOUT_S` | `300` | сколько ждать YES от owner-а |
| `ORCH_LOG_LEVEL` | `INFO` | DEBUG/INFO/WARNING |
| `ORCH_LOG_TO_L1` | `1` | писать ли решения в L1 |
| `ORCH_DRY_RUN` | `0` | если `1` — actions не выполняются (только лог) |

## Ops

```bash
# Статус
systemctl status cogcore-orchestrator

# Логи (live)
journalctl -u cogcore-orchestrator -f

# Логи (последние 100 строк)
journalctl -u cogcore-orchestrator -n 100 --no-pager

# Restart (после изменения env)
sudo systemctl restart cogcore-orchestrator

# Disable (временно)
sudo systemctl stop cogcore-orchestrator
sudo systemctl disable cogcore-orchestrator
```

### Audit trail

Все решения orchestrator-а пишутся в L1 → запрос через psql или cognitive_recall:

```bash
docker exec cognitive_postgres psql -U cognitive -d cognitive_core -c "
  SELECT timestamp, raw_payload->>'action' AS action, raw_payload->>'source_agent' AS source
    FROM l1_raw_events
   WHERE domain = 'orchestrator_decisions'
   ORDER BY timestamp DESC
   LIMIT 20;
"
```

## Безопасность

1. **API key** orchestrator-а хранится в `/etc/cogcore-orchestrator.env` с
   chmod 600. НЕ коммитим в репо, НЕ печатаем в логах.
2. **Hardcoded whitelist actions** — DeepSeek не может выполнить произвольный
   код, только то что в `ACTIONS` dict.
3. **Approval gate hardcoded** — `DESTRUCTIVE_ACTIONS` — это frozenset в коде,
   DeepSeek не может его обойти.
4. **Anti-loop** — DM от себя самого или от `server-runtime` игнорируются.
5. **Non-owner destructive REFUSED** — только agent_id из `OWNER_AGENT_ID`
   может инициировать удаление/revoke.
6. **Soft-delete only** — даже после approval, `delete_agent` / `revoke_key`
   / `purge_data` только пишут soft-marker в L1; hard-delete требует ручной
   SQL операции admin-ом. Это design choice — защита от полного wipe через
   одну команду.

## Edge cases

- **DeepSeek упал**: orchestrator отвечает `Не смог разобрать команду
  (LLM недоступен).` и продолжает работу.
- **Невалидный JSON от DeepSeek**: orchestrator отвечает «не понял».
- **Команда вне whitelist**: action='refuse' с объяснением.
- **Не хватает args (например «передай задачу» без агента)**:
  action='request_clarification' — orchestrator задаёт уточняющий вопрос.
- **Timeout approval**: 5 мин без ответа → действие НЕ выполняется,
  source получает «approval timeout».

## Дальнейшее развитие

- [ ] Push-based: вместо polling — LISTEN на PG NOTIFY канале
  `agent_inbox_new` (есть уже `cognitive-pg-to-nats.service`)
- [ ] Tool-calling: вместо JSON-парсинга — нативный DeepSeek function calling
  (как в `cognitive-agent-runtime.py` v2)
- [ ] Multi-step plans: orchestrator может выполнять цепочки действий
  («посчитай у Растра X, потом передай результат Y»)
- [ ] Hard-delete: добавить admin endpoint `/agents/{id}/hard-delete` который
  orchestrator может вызвать после approval
- [ ] Multi-owner: разрешить approval не только одному owner-у, а группе
  (например trustees)

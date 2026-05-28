# me-ai-edge — локальная обработка медиа (Edge Architecture)

> Статус: **0.1 (stub)** — каркас контейнера и API. Реальная обработка медиа
> добавляется в 0.2 (следующий PR). Это design doc для M4 в v1.0 roadmap.

## TL;DR

`me-ai-edge` — лёгкий docker-контейнер, который запускается **на машине агента**
и обрабатывает медиа (видео/аудио/изображения) **локально**: ffmpeg извлекает
кадры, Whisper транскрибирует речь, локальная модель считает embeddings. В
облачный Cognitive Core уходят **только производные данные** — текст транскрипта,
векторы embeddings и метаданные. **Raw-байты медиа и кадры никогда не покидают
локальную машину.**

Это закрывает требование security-strict тенантов, у которых PII (лица, голоса,
номера документов) внутри медиа не может пересекать периметр.

---

## Зачем это нужно

Сегодня весь media-pipeline Cognitive Core работает на сервере: агент загружает
raw-байты наружу (`cognitive_media_upload`), сервер гоняет ffmpeg + faster-whisper,
сохраняет кадры в MinIO и пишет метаданные в L1. Для большинства пользователей
это удобно и дёшево.

Но есть класс тенантов, которым это **запрещено политикой**:

| Сегмент | Почему raw-медиа нельзя наружу |
|---------|--------------------------------|
| Government / гос-сектор | 152-ФЗ, гостайна, данные в кадрах не покидают контур |
| Military / оборонка | Air-gapped периметр, любой outbound с raw-данными — инцидент |
| Medical / здравоохранение | PHI/врачебная тайна: лица пациентов, голоса в видеоконсультациях |
| Legal / финансы | NDA-материалы, persons-of-interest, документы в кадре |

Для них единственный приемлемый вариант — **обрабатывать медиа локально** и
отдавать наверх только обезличенные производные (вектора + текст). Именно это
делает `me-ai-edge`.

---

## Архитектура

```
┌──────────────────────── машина агента (on-prem / периметр) ───────────────────┐
│                                                                                │
│   ┌─────────┐   raw video/audio    ┌──────────────────────────┐               │
│   │  Агент  │ ───── (localhost) ──▶ │  me-ai-edge  :9099        │               │
│   │ (Claude │                       │  (docker, 127.0.0.1 only)│               │
│   │  Code)  │ ◀── transcript/meta ──│                          │               │
│   └─────────┘                       │  • ffmpeg → frames       │               │
│                                     │  • faster-whisper → text │               │
│                                     │  • local embed → vectors │               │
│                                     └────────────┬─────────────┘               │
│                                                  │                             │
└──────────────────────────────────────────────── │ ────────────────────────────┘
                                                   │  HTTPS POST
                                                   │  embeddings + transcript + metadata
                                                   │  Authorization: Bearer <API key>
                                                   ▼
                                  ┌────────────────────────────────┐
                                  │  Cognitive Core (cloud)         │
                                  │  https://mcp.me-ai.ru           │
                                  │  POST /api/embeddings/ingest    │
                                  │  • валидирует API key per-agent │
                                  │  • пишет vectors в pgvector     │
                                  │  • recall работает как обычно   │
                                  └────────────────────────────────┘
```

Ключевая граница: **всё, что слева от пунктира, остаётся на машине агента.**
Наружу пересекает периметр только HTTPS POST с обезличенными производными.

### Поток данных (0.2, целевой)

1. Агент отдаёт raw-байты медиа edge-контейнеру по `http://127.0.0.1:9099/process/video`
   (multipart upload, не покидает loopback-интерфейс).
2. Edge извлекает кадры и аудио через ffmpeg во временный tmpfs.
3. `faster-whisper` локально транскрибирует речь (CPU int8, как на сервере).
4. Локальная embed-модель считает вектора из transcript + кадров.
5. Edge делает **HTTPS POST** на `COGCORE_SERVER_URL/api/embeddings/ingest`,
   передавая `Authorization: Bearer <COGCORE_API_KEY>`. В теле — **только**
   embeddings, текст транскрипта и метаданные.
6. Временные кадры и аудио удаляются. Raw-видео не сохраняется и не отправляется.

---

## Trust model

- **Edge держит API-ключ локально.** `COGCORE_API_KEY` — это per-agent ключ,
  тот же механизм, что уже используется в Cognitive Core. Он не хешируется на
  edge и не отправляется никуда, кроме upstream-ingest.
- **Сервер валидирует per-agent.** Cognitive Core принимает embeddings только
  с валидным ключом и атрибутирует их конкретному агенту/owner (изоляция через
  `owner_user_id`, Phase 4). Edge не получает привилегий — это просто клиент.
- **Anti-exfil whitelist.** `ALLOWED_UPSTREAM_DOMAINS` — список доменов, на
  которые edge вправе слать данные. Даже если `COGCORE_SERVER_URL` подменят на
  чужой хост, домен не пройдёт проверку и отправка будет заблокирована. По
  умолчанию — только `mcp.me-ai.ru` и его IDN-алиас.
- **Localhost-only bind.** Контейнер публикует порт только на `127.0.0.1:9099`.
  Edge физически недоступен из локальной сети — обращаться к нему может лишь
  агент на том же хосте. Никакого inbound с других машин.
- **Transport.** Upstream только по HTTPS (TLS termination на nginx Cognitive
  Core). Между агентом и edge — loopback, шифрование не требуется.

---

## Что НЕ покидает локальную машину

- Raw audio-байты (mp3/wav/ogg/m4a и аудиодорожка видео)
- Raw video-байты (mp4/mov/webm/…)
- Извлечённые JPEG-кадры
- Любые промежуточные временные файлы (tmpfs, удаляются после обработки)

## Что загружается в облако

| Данные | Пример | Зачем серверу |
|--------|--------|---------------|
| Текст транскрипта | `"обсудили бюджет на Q3…"` | Полнотекстовый recall, контекст |
| Embeddings (vectors) | `float32[1024]` | Семантический поиск (pgvector) |
| Metadata | `duration`, `codec`, `frame_count`, `language`, `has_audio` | Фильтрация, аналитика |

> Транскрипт — это текст, а не raw-медиа. Для большинства security-режимов это
> приемлемо (текст обезличивается отдельно при необходимости). Для air-gapped
> сценариев, где даже транскрипт не должен уходить, см. 0.3 (локальный режим
> без upstream).

---

## Roadmap

| Версия | Объём | Статус |
|--------|-------|--------|
| **0.1 stub** | Каркас: Dockerfile, FastAPI `:9099`, `/health`, `/process/*` заглушки, конфиг, anti-exfil whitelist | **Этот PR** |
| **0.2 full** | Реальная обработка: ffmpeg-кадры + Whisper-транскрипт + локальные embeddings + `POST /api/embeddings/ingest` upstream | Следующий PR |
| **0.3 enterprise** | mTLS клиент-сертификаты, локальный audit-log всех обработок, air-gapped режим (вообще без upstream — только локальный индекс), GPU-профиль | Позже |

---

## Deploy

На машине агента:

```bash
# 1. Положить edge/ рядом (Dockerfile, main.py, docker-compose.yml)
cd edge/

# 2. Задать конфиг (или экспортировать env заранее)
export COGCORE_SERVER_URL="https://mcp.me-ai.ru"
export COGCORE_API_KEY="<per-agent-key>"
export WHISPER_MODEL_SIZE="base"   # tiny|base|small|medium|large

# 3. Поднять
docker compose up -d --build

# 4. Проверить
curl -s http://127.0.0.1:9099/health | jq
# → {"healthy": true, "version": "0.1.0-stub",
#    "server_configured": true, "whisper_model": "base"}
```

Агент после этого направляет медиа на `http://127.0.0.1:9099/process/*` вместо
облачного `cognitive_media_upload`.

---

## Конфигурация (env vars)

| Переменная | Обязательна | Default | Назначение |
|------------|-------------|---------|------------|
| `COGCORE_SERVER_URL` | да | — | Базовый URL облачного Cognitive Core для upstream-ingest |
| `COGCORE_API_KEY` | да | — | Per-agent API-ключ. Хранится локально, шлётся в `Authorization` upstream |
| `WHISPER_MODEL_SIZE` | нет | `base` | Размер модели Whisper: `tiny`/`base`/`small`/`medium`/`large` |
| `ALLOWED_UPSTREAM_DOMAINS` | нет | `mcp.me-ai.ru,mcp.xn--80aiacb7adkj.xn--p1ai` | CSV-whitelist доменов, куда edge вправе отправлять данные (anti-exfil) |

Whisper-веса кэшируются в named volume `whisper_cache` (`/data/whisper`) и
переиспользуются между рестартами — повторного скачивания нет.

---

## Cloud pipeline vs Edge — сравнение

| Критерий | Cloud media pipeline (текущий) | me-ai-edge (этот компонент) |
|----------|--------------------------------|-----------------------------|
| Где обрабатывается | На сервере Cognitive Core | На машине агента (on-prem) |
| Raw-байты наружу | Да (upload в MinIO) | **Нет** (только производные) |
| Privacy / compliance | Общий случай | gov / military / medical / legal |
| Latency | Сеть + очередь на сервере | Локальный CPU, без сетевого upload медиа |
| Сетевой трафик | Тяжёлый (весь файл) | Лёгкий (текст + вектора, килобайты) |
| Стоимость CPU | На стороне Cognitive Core | На стороне агента (его железо) |
| GPU | Доступен серверный профиль | CPU int8 по умолчанию (GPU в 0.3) |
| Хранение кадров | MinIO `media-frames` | Локально, удаляются после обработки |
| Air-gapped | Невозможно | Возможно в 0.3 (без upstream) |

Edge — это **trade-off privacy↔convenience**: тенант берёт CPU-нагрузку и
self-host на себя в обмен на гарантию, что raw-медиа не покидает периметр.

---

## Текущие ограничения (0.1 stub)

- `/process/video`, `/process/audio`, `/process/image` — **заглушки**: возвращают
  `{"status": "stub_not_implemented", "todo": "M4 next PR"}` с описанием будущего
  flow. Реальной обработки нет.
- ffmpeg и `faster-whisper` уже устанавливаются в образ (слой готов), но
  pipeline-код подключается в 0.2.
- Upstream `POST /api/embeddings/ingest` ещё не вызывается (endpoint на стороне
  сервера — отдельная задача 0.2).
- mTLS, audit-log и air-gapped режим — в 0.3.
- GPU не используется (CPU int8). GPU-профиль — в 0.3.

# Cognitive Core — Market Analysis

> Положение на рынке AI-памяти, дифференциаторы, целевая аудитория, потенциал монетизации.
> Обновляется каждые 6 месяцев.

## TL;DR

**Cognitive Core занимает уникальную нишу:** self-hosted multi-layer memory с AI-куратором, аудит-логом и multi-language. Никто из конкурентов (Mem0, Letta, Zep, OpenAI Memory) не сочетает все 5 ключевых свойств.

**Целевая аудитория:** regulated industries (финансы, медицина, госсектор) + AI-стартапы 5-50 человек, которым нужна data sovereignty.

**Revenue model (по DeepSeek):** Open Core + Enterprise tier (как GitLab/Mattermost).

## Карта конкурентов на 2026

| Решение | Stars | Тип | Сильное | Слабое |
|---|---|---|---|---|
| **Mem0** | 30k+ | OSS + cloud | Простота API, community | Плоская память, нет audit, нет консолидации |
| **Letta (MemGPT)** | 15k+ | OSS agent runtime | Memory blocks, OS-метафора | Heavy, привязан к runtime, сложен |
| **Zep + Graphiti** | ~5k | OSS + paid cloud | Bi-temporal graph | Тяжелый стек, vendor-lean, paid for prod |
| **OpenAI Memory** | (closed) | Proprietary | Zero-config UX | Только в ChatGPT |
| **Anthropic Memory tools** | (API) | Низкоуровневый | Simple files API | Нет архитектуры, manual |
| **Cognee** | ~3k | OSS knowledge graph | ECL pipeline | Молодой, мало adapters |
| **LangMem** | (lib) | LangChain | Profile/episodic/semantic | Привязка к LangGraph |

## Уникальное позиционирование Cognitive Core

```
                    ОСЬ Y: глубина консолидации
                          ↑
                          │ много слоёв
                          │
                  Letta ●     ⭐ Cognitive Core
                Zep ●            (мы здесь)
                          │
                          │
        OpenAI Memory ●     Mem0 ●  Memori ●
        Anthropic tool ●         Cognee ●
                          │ плоская
                          └─────────────────────────►
                            ОСЬ X: уровень контроля
                            Closed → Cloud → Self-hosted
```

**Никто из конкурентов не сочетает все 5 свойств:**

1. ✅ Multi-layer consolidation (5 слоёв L1→L4 + OP)
2. ✅ AI-куратор как gate quality на каждом переходе
3. ✅ Self-hosted одной командой (`docker compose up`)
4. ✅ Полный audit log L5
5. ✅ Multi-language (8 языков)

## Целевая аудитория

| Сегмент | Размер | Use case | Как достучаться |
|---|---|---|---|
| **Enterprise (regulated)** | Tier-1: финансы, медицина, госсектор | Compliance + data sovereignty + audit | Long sales cycle, через consultancy |
| **AI-стартапы 5-50 человек** | Tier-2: 10k+ команд | Persistent memory для команды агентов | GitHub publish + Reddit + HackerNews |
| **Solo AI-разработчики** | Tier-3: 100k+ человек | Личная память для Claude/Cursor | Twitter, video demos, Cherry Studio |
| **Researchers** | Tier-4: universities | Эксперименты с multi-layer memory | Papers, conferences |

**По DeepSeek-анализу**: «Cognitive Core's 5-layer pipeline with audit log, quality gates, and ring retention serves regulated industries (finance, healthcare) where traceability and control are mandatory.»

## Угрозы

| Угроза | Вероятность | Защита |
|---|---|---|
| Anthropic/OpenAI выпускают managed memory | Высокая | Self-hosted = data sovereignty (governance compliance) |
| Mem0 добавляет multi-layer | Средняя | Opportunity cost для них; мы глубже |
| Большие LLM получают 10M+ context | Высокая | Context ≠ structured memory; LLM-память opaque |
| Кто-то форкнет и обгонит | Низкая | Скорость итераций — наше преимущество |

## Возможности роста

| Возможность | Размер | Срочность |
|---|---|---|
| Multi-language (russian, chinese, arabic markets) | 200M+ людей | Высокая — мало конкурентов |
| GDPR/SOC2 compliance niche | Растёт | Средняя |
| AI-агенты для regulated industries | Очень растёт в 2026-27 | Высокая |
| Multimodal memory layer | Все будут делать | Средняя — пока никто не делает с ring retention |
| **Tools Index** (independent benchmark) | Open ecosystem | Средняя — может стать "PyPI для AI tools" |

## Revenue Model — Open Core + Enterprise

DeepSeek-validated: **Open Core (как GitLab, Mattermost, Sentry).**

```
┌─ FREE / OSS ──────────────────────────────────────┐
│  • 5-layer memory                                  │
│  • MCP server (stdio + SSE)                        │
│  • Self-hosted docker-compose                      │
│  • До 5 agents                                     │
│  • Community support через GitHub                 │
└────────────────────────────────────────────────────┘

┌─ ENTERPRISE TIER ($500-2000/mo) ─────────────────┐
│  • Unlimited agents                                │
│  • SSO (SAML, OIDC)                                │
│  • RBAC (роли + permissions)                       │
│  • Advanced audit (export для compliance)          │
│  • Priority support + 99.9% SLA                    │
│  • Multi-tenancy                                   │
│  • Cold storage tier для архива                    │
└────────────────────────────────────────────────────┘

┌─ MANAGED HOSTING ($100-500/mo) ──────────────────┐
│  • Cognitive Core managed by us                    │
│  • Auto-updates, backups, monitoring               │
│  • TLS + custom domain                             │
│  • 24/7 ops support                                │
└────────────────────────────────────────────────────┘
```

**Не делать:** marketplace (преждевременно), freemium cloud (размывает self-hosted USP).

## Метрики успеха

| Stage | Метрика | Цель | Срок |
|---|---|---|---|
| **Distribution v0** | GitHub stars | 100 | 1 месяц после publish |
| **Validation** | First production user | 1+ pilot | 2 месяца |
| **Community** | External contributors | 5+ | 6 месяцев |
| **Enterprise pilot** | Paying customer | 1+ | 9 месяцев |
| **Mass adoption** | Recurring revenue | $10k MRR | 18 месяцев |

## Конкурентное преимущество (durability)

Что **не отнимут** конкуренты в течение 12-24 месяцев:

1. **Архитектура multi-layer** — копировать долго (это не "добавить колонку")
2. **AI-куратор как философия** — другие команды думают по-другому
3. **Multi-language прорыв** — Mem0/Letta англоязычные команды, локализация для них стоит дорого
4. **Self-hosted "из коробки"** — Letta/Zep делают heavy stack
5. **Russian-speaking community** — потенциал huge (Yandex, Ozon, Kaspersky как ICP)

## Стратегия запуска

### Phase 1 (Месяц 1-2): Validate
- Server deploy на личном VPS
- Dogfooding 2 недели
- Найти реальные friction
- Закрыть критические

### Phase 2 (Месяц 3): Public
- GitHub publish v0.6
- README с screenshots + GIF demo
- Reddit r/LocalLLaMA, Hacker News, X
- Цель: 100 stars + 5 issues

### Phase 3 (Месяц 4-6): Differentiate
- Бенчмарк vs Mem0/Zep по recall@k
- Multimodal memory (vision-LLM)
- Universal ring retention
- Цель: 500 stars + первые external contributors

### Phase 4 (Месяц 7-12): Monetize
- Найти 2-3 enterprise pilot
- Open core split: SSO/RBAC/advanced audit
- Managed hosting tier
- Цель: $10k MRR

### Phase 5 (Год 2+): Scale
- Multi-tenancy
- Worker как отдельный сервис
- Helm chart, Kubernetes
- Цель: $100k MRR, 5k+ stars

## Риски монетизации

| Риск | Митигация |
|---|---|
| Open core воспринимается как «бейт-эс-свитч» | Чётко документировать что в free vs enterprise |
| Конкуренция от free Mem0/Letta | Дифференциация архитектурой, не фичами |
| Слишком быстрая переход на enterprise отталкивает OSS | Поддерживать активный free tier 2+ лет |

## Связь с другими документами

- [`README.md`](README.md) — top-level pitch
- [`COMPARISON.md`](COMPARISON.md) — детальное сравнение features
- [`roadmap.md`](roadmap.md) — план реализации
- [`SECURITY.md`](SECURITY.md) — compliance positioning

## Источники анализа

- DeepSeek consultations (см. `scripts/deepseek_out/consult_*.json`)
- GitHub stats Mem0, Letta, Zep на 2026-05
- Известные revenue models open-source DevTools (GitLab, Mattermost, Sentry, n8n)

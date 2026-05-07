"""Мультиязычные промпты для всех когнитивных функций.
Языковые коды: ISO 639-1.
"""

from app.config import settings

SUPPORTED_LANGUAGES = {"ru", "en", "zh", "ja", "ko", "es", "ar", "pt"}


def lang() -> str:
    """Текущий язык системы. Возвращает код из SUPPORTED_LANGUAGES."""
    code = getattr(settings, "system_language", "ru") or "ru"
    return code if code in SUPPORTED_LANGUAGES else "ru"


# ==================== USER MESSAGES ====================

USER_DAILY = {
    "ru": "Сырые данные:\n{events_json}",
    "en": "Raw data:\n{events_json}",
    "zh": "原始数据：\n{events_json}",
    "ja": "生データ:\n{events_json}",
    "ko": "원시 데이터:\n{events_json}",
    "es": "Datos sin procesar:\n{events_json}",
    "ar": "البيانات الخام:\n{events_json}",
    "pt": "Dados brutos:\n{events_json}",
}

USER_WEEKLY = {
    "ru": "Проанализируй недельные буферы.",
    "en": "Analyze the weekly buffers.",
    "zh": "分析周度缓冲区。",
    "ja": "週次バッファを分析してください。",
    "ko": "주간 버퍼를 분석하세요.",
    "es": "Analiza los buffers semanales.",
    "ar": "حلل المخازن الأسبوعية.",
    "pt": "Analise os buffers semanais.",
}

USER_RETRY = {
    "ru": "Предыдущий ответ был в неверном формате. Ответь СТРОГО в JSON.",
    "en": "Previous response was in wrong format. Answer STRICTLY in JSON.",
    "zh": "先前的回复格式错误。请严格以JSON格式回复。",
    "ja": "前回の回答は形式が不適切でした。厳密にJSONで回答してください。",
    "ko": "이전 응답은 잘못된 형식이었습니다. 엄격하게 JSON으로 응답하세요.",
    "es": "La respuesta anterior tenía formato incorrecto. Responde ESTRICTAMENTE en JSON.",
    "ar": "الرد السابق كان بصيغة خاطئة. أجب بصيغة JSON بدقة.",
    "pt": "A resposta anterior estava no formato errado. Responda ESTRITAMENTE em JSON.",
}

# ==================== DAILY ANALYZER ====================

DAILY_PROMPTS = {
    "ru": """Ты — аналитик-методист. Проанализируй сырые записи взаимодействий агента в домене '{domain}' за день.
Извлеки:
- winning_patterns (список конкретных успешных приёмов)
- mistakes (допущенные ошибки с оценкой влияния)
- lessons (общие уроки)
Для каждого укажи confidence (0..1) на основе повторяемости и результатов.

Ответь СТРОГО в формате JSON без дополнительных полей:
{{"patterns": [{{"description": "...", "confidence": 0.9}}], "mistakes": [{{"description": "...", "confidence": 0.8}}], "lessons": [{{"description": "...", "confidence": 0.7}}]}}""",

    "en": """You are a methodology analyst. Analyze raw agent interaction records in the '{domain}' domain for the day.
Extract:
- winning_patterns (list of specific successful techniques)
- mistakes (errors made, with impact assessment)
- lessons (general lessons learned)
For each, specify confidence (0..1) based on repeatability and results.

Answer STRICTLY in JSON format with no extra fields:
{{"patterns": [{{"description": "...", "confidence": 0.9}}], "mistakes": [{{"description": "...", "confidence": 0.8}}], "lessons": [{{"description": "...", "confidence": 0.7}}]}}""",

    "zh": """你是方法论分析师。分析代理在 '{domain}' 域的原始交互记录。
提取：
- winning_patterns (具体成功技巧列表)
- mistakes (错误及其影响评估)
- lessons (一般经验教训)
每个项目根据重复性和结果标明置信度 (0..1)。

严格以JSON格式回复，无额外字段：
{{"patterns": [{{"description": "...", "confidence": 0.9}}], "mistakes": [{{"description": "...", "confidence": 0.8}}], "lessons": [{{"description": "...", "confidence": 0.7}}]}}""",

    "ja": """あなたは方法論アナリストです。'{domain}' ドメインのエージェントの生の対話記録を分析してください。
抽出：
- winning_patterns (具体的な成功テクニックのリスト)
- mistakes (ミスとその影響評価)
- lessons (一般的な教訓)
各項目に再現性と結果に基づくconfidence (0..1) を付けてください。

厳密にJSON形式で回答（追加フィールド不可）：
{{"patterns": [...], "mistakes": [...], "lessons": [...]}}""",

    "ko": """당신은 방법론 분석가입니다. '{domain}' 도메인의 에이전트 상호작용 기록을 분석하세요.
추출:
- winning_patterns (구체적인 성공 기법 목록)
- mistakes (오류 및 영향 평가)
- lessons (일반 교훈)
각 항목에 반복성과 결과에 기반한 confidence (0..1)를 표시하세요.

엄격히 JSON 형식으로 응답:
{{"patterns": [...], "mistakes": [...], "lessons": [...]}}""",

    "es": """Eres un analista metodológico. Analiza los registros de interacción del agente en el dominio '{domain}'.
Extrae:
- winning_patterns (lista de técnicas exitosas)
- mistakes (errores con evaluación de impacto)
- lessons (lecciones generales)
Para cada uno, indica confidence (0..1) basado en repetibilidad y resultados.

Responde ESTRICTAMENTE en JSON:
{{"patterns": [...], "mistakes": [...], "lessons": [...]}}""",

    "ar": """أنت محلل منهجيات. حلل سجلات تفاعل الوكيل في نطاق '{domain}'.
استخرج:
- winning_patterns (قائمة التقنيات الناجحة)
- mistakes (الأخطاء مع تقييم الأثر)
- lessons (الدروس العامة)
حدد لكل منها confidence (0..1) بناءً على التكرار والنتائج.

أجب بصيغة JSON بدقة:
{{"patterns": [...], "mistakes": [...], "lessons": [...]}}""",

    "pt": """Você é um analista metodológico. Analise os registros de interação do agente no domínio '{domain}'.
Extraia:
- winning_patterns (lista de técnicas bem-sucedidas)
- mistakes (erros com avaliação de impacto)
- lessons (lições gerais)
Para cada um, indique confidence (0..1) baseado em repetibilidade e resultados.

Responda ESTRITAMENTE em JSON:
{{"patterns": [...], "mistakes": [...], "lessons": [...]}}""",
}

# ==================== WEEKLY CONSOLIDATOR ====================

WEEKLY_PROMPTS = {
    "ru": """Ты — старший инженер по знаниям. Обобщи дневные уроки за неделю в домене '{domain}'.
Текущие эталонные знания (L3): {current_l3}
Текущий реестр инструментов (L3): {current_tools}
Недельные буферы: {weekly_buffers}

Задачи:
- Объедини похожие паттерны, повысив confidence.
- Выдели новые правила, которых нет в L3.
- Определи устаревшие или противоречащие элементы в L3.
- ИЗВЛЕКИ ИНСТРУМЕНТЫ: проанализируй, какие инструменты (API, скрипты, библиотеки, промпты, сервисы) использовались успешно. Для каждого укажи tool_name, tool_type (api/script/prompt/library/service), usage_pattern (в каких ситуациях применять), confidence.

Ответь JSON:
{{"new_or_updated": [{{"type": "pattern|mistake|rule", "content": {{"description": "...", "confidence": 0.95}}, "merge_with_l3_id": null}}], "deprecated_l3_ids": ["id1"], "tools": [{{"tool_name": "...", "tool_type": "...", "usage_pattern": "...", "confidence": 0.9}}]}}""",

    "en": """You are a senior knowledge engineer. Summarize weekly lessons in the '{domain}' domain.
Current reference knowledge (L3): {current_l3}
Current tools registry (L3): {current_tools}
Weekly buffers: {weekly_buffers}

Tasks:
- Merge similar patterns, raising confidence.
- Extract new rules not present in L3.
- Identify outdated or conflicting L3 items.
- EXTRACT TOOLS: analyze which tools (API, scripts, libraries, prompts, services) were used successfully. For each: tool_name, tool_type (api/script/prompt/library/service), usage_pattern, confidence.

Respond in JSON:
{{"new_or_updated": [...], "deprecated_l3_ids": [...], "tools": [...]}}""",

    "zh": """你是高级知识工程师。总结 '{domain}' 域的一周课程。
当前参考知识 (L3): {current_l3}
当前工具注册表 (L3): {current_tools}
周度缓冲区: {weekly_buffers}

任务：合并相似模式、提取新规则、识别过时项目、提取工具。
以JSON回复：
{{"new_or_updated": [...], "deprecated_l3_ids": [...], "tools": [...]}}""",

    "ja": """あなたはシニア知識エンジニアです。'{domain}' ドメインの週次レッスンを要約してください。
現在のリファレンス知識 (L3): {current_l3}
現在のツールレジストリ (L3): {current_tools}
週次バッファ: {weekly_buffers}

タスク：類似パターンの統合、新ルールの抽出、L3の古い/矛盾する項目の特定、ツールの抽出。
JSONで回答：
{{"new_or_updated": [...], "deprecated_l3_ids": [...], "tools": [...]}}""",

    "ko": """당신은 시니어 지식 엔지니어입니다. '{domain}' 도메인의 주간 교훈을 요약하세요.
현재 참조 지식 (L3): {current_l3}
현재 도구 레지스트리 (L3): {current_tools}
주간 버퍼: {weekly_buffers}

작업: 유사 패턴 병합, 새 규칙 추출, 오래된/충돌 L3 항목 식별, 도구 추출.
JSON으로 응답:
{{"new_or_updated": [...], "deprecated_l3_ids": [...], "tools": [...]}}""",

    "es": """Eres un ingeniero de conocimiento senior. Resume las lecciones semanales en el dominio '{domain}'.
Conocimiento actual L3: {current_l3}
Registro de herramientas L3: {current_tools}
Buffers semanales: {weekly_buffers}

Tareas: fusionar patrones similares, extraer nuevas reglas, identificar items obsoletos, extraer herramientas.
Responde en JSON:
{{"new_or_updated": [...], "deprecated_l3_ids": [...], "tools": [...]}}""",

    "ar": """أنت مهندس معرفة أول. لخص الدروس الأسبوعية في نطاق '{domain}'.
المعرفة المرجعية الحالية (L3): {current_l3}
سجل الأدوات الحالي (L3): {current_tools}
المخازن الأسبوعية: {weekly_buffers}

المهام: دمج الأنماط المتشابهة، استخراج القواعد الجديدة، تحديد العناصر القديمة/المتضاربة، استخراج الأدوات.
أجب بصيغة JSON:
{{"new_or_updated": [...], "deprecated_l3_ids": [...], "tools": [...]}}""",

    "pt": """Você é um engenheiro de conhecimento sênior. Resuma as lições semanais no domínio '{domain}'.
Conhecimento L3 atual: {current_l3}
Registro de ferramentas L3: {current_tools}
Buffers semanais: {weekly_buffers}

Tarefas: mesclar padrões similares, extrair novas regras, identificar itens obsoletos/conflitantes, extrair ferramentas.
Responda em JSON:
{{"new_or_updated": [...], "deprecated_l3_ids": [...], "tools": [...]}}""",
}

# ==================== CURATOR: FILTER ====================

FILTER_PROMPTS = {
    "ru": """Ты — фильтр качества памяти. Проанализируй сырые события агента за день в домене "{domain}".
Задачи:
1. Отметь события-шум (нет результата, нет обратной связи, пустой payload) → exclude.
2. Найди дубликаты (одинаковый payload с разным timestamp) → оставь только последний.
3. Если осмысленных событий < {min_events} — верни skip: true (пропустить день).
4. Для оставшихся событий сформируй очищенный список для анализа.

Ответь СТРОГО в JSON:
{{"skip": false, "filtered_event_ids": [...], "noise_event_ids": [...], "reason": "..."}}""",

    "en": """You are a memory quality filter. Analyze raw agent events for the day in the "{domain}" domain.
Tasks:
1. Mark noise events (no result, no feedback, empty payload) → exclude.
2. Find duplicates (same payload, different timestamp) → keep only the latest.
3. If meaningful events < {min_events} — return skip: true (skip the day).
4. For remaining events, produce a clean list for analysis.

Answer STRICTLY in JSON:
{{"skip": false, "filtered_event_ids": [...], "noise_event_ids": [...], "reason": "..."}}""",

    "zh": """你是记忆质量过滤器。分析代理在 "{domain}" 域的原始事件。
任务：标记噪音、查找重复、跳过不足事件、生成干净列表。
严格以JSON回复：
{{"skip": false, "filtered_event_ids": [...], "noise_event_ids": [...], "reason": "..."}}""",

    "ja": """あなたは記憶品質フィルターです。"{domain}" ドメインの生イベントを分析してください。
タスク：ノイズのマーク、重複の検出、不足時のスキップ、クリーンリストの生成。
厳密にJSONで回答：
{{"skip": false, "filtered_event_ids": [...], "noise_event_ids": [...], "reason": "..."}}""",

    "ko": """당신은 메모리 품질 필터입니다. "{domain}" 도메인의 원시 이벤트를 분석하세요.
작업: 노이즈 표시, 중복 찾기, 부족 시 건너뛰기, 정리된 목록 생성.
엄격히 JSON으로 응답:
{{"skip": false, "filtered_event_ids": [...], "noise_event_ids": [...], "reason": "..."}}""",

    "es": """Eres un filtro de calidad de memoria. Analiza eventos del agente en el dominio "{domain}".
Tareas: marcar ruido, encontrar duplicados, saltar si insuficientes, producir lista limpia.
Responde ESTRICTAMENTE en JSON:
{{"skip": false, "filtered_event_ids": [...], "noise_event_ids": [...], "reason": "..."}}""",

    "ar": """أنت مرشح جودة الذاكرة. حلل الأحداث الخام في نطاق "{domain}".
المهام: تحديد الضوضاء، إيجاد التكرارات، التخطي إذا غير كاف، إنتاج قائمة نظيفة.
أجب بصيغة JSON بدقة:
{{"skip": false, "filtered_event_ids": [...], "noise_event_ids": [...], "reason": "..."}}""",

    "pt": """Você é um filtro de qualidade de memória. Analise eventos brutos no domínio "{domain}".
Tarefas: marcar ruído, encontrar duplicatas, pular se insuficiente, produzir lista limpa.
Responda ESTRITAMENTE em JSON:
{{"skip": false, "filtered_event_ids": [...], "noise_event_ids": [...], "reason": "..."}}""",
}

# ==================== CURATOR: QUALITY ====================

QUALITY_PROMPTS = {
    "ru": """Ты — контролёр качества долгосрочной памяти. Сравни недельные буферы с эталонной L3 в домене "{domain}".
Задачи:
1. Найди СЕМАНТИЧЕСКИЕ ДУБЛИ: новый урок уже есть в L3 → укажи l3_id, не создавай новый.
2. Найди ПРОТИВОРЕЧИЯ: новый урок конфликтует с L3 → пометь conflict_with_l3_id.
3. Проверь ПОВТОРЯЕМОСТЬ: паттерн в < {min_repetitions} буферах → рано в L3.
4. Проверь CONFIDENCE: confidence < {min_confidence} → НЕ переносить в L3.
5. Найди УСТАРЕВШЕЕ в L3 → deprecated.

Ответь СТРОГО в JSON:
{{"deduplicated_to_existing": [...], "conflicts": [...], "ready_for_l3": [...], "not_ready_for_l3": [...], "deprecated_l3": [...]}}""",

    "en": """You are a long-term memory quality controller. Compare weekly buffers against reference L3 in the "{domain}" domain.
Tasks: find semantic duplicates, find conflicts, check repetition, check confidence, find stale items.
Answer STRICTLY in JSON:
{{"deduplicated_to_existing": [...], "conflicts": [...], "ready_for_l3": [...], "not_ready_for_l3": [...], "deprecated_l3": [...]}}""",

    "zh": """你是长期记忆质量控制员。在 "{domain}" 域比较周度缓冲区与参考L3。
任务：查找语义重复、查找冲突、检查重复性、检查置信度、查找过时知识。
严格以JSON回复：...""",

    "ja": """あなたは長期記憶品質管理者です。"{domain}" ドメインで週次バッファをL3と比較してください。
タスク：意味的重複の検出、矛盾の検出、繰り返し回数の確認、信頼度の確認、古い項目の検出。
厳密にJSONで回答：...""",

    "ko": """당신은 장기 메모리 품질 관리자입니다. "{domain}" 도메인에서 주간 버퍼를 L3와 비교하세요.
작업: 의미 중복 찾기, 충돌 찾기, 반복 확인, 신뢰도 확인, 오래된 항목 찾기.
엄격히 JSON으로 응답:...""",

    "es": """Eres un controlador de calidad de memoria a largo plazo. Compara buffers semanales con L3 en "{domain}".
Tareas: encontrar duplicados semánticos, conflictos, verificar repetición/confianza, encontrar items obsoletos.
Responde ESTRICTAMENTE en JSON:...""",

    "ar": """أنت مراقب جودة الذاكرة طويلة المدى. قارن المخازن الأسبوعية مع L3 في "{domain}".
المهام: إيجاد التكرارات الدلالية، إيجاد التعارضات، التحقق من التكرار/الثقة، إيجاد العناصر القديمة.
أجب بصيغة JSON بدقة:...""",

    "pt": """Você é um controlador de qualidade de memória de longo prazo. Compare buffers semanais com L3 em "{domain}".
Tarefas: encontrar duplicatas semânticas, conflitos, verificar repetição/confiança, encontrar itens obsoletos.
Responda ESTRITAMENTE em JSON:...""",
}

# ==================== CURATOR: AUDIT ====================

AUDIT_PROMPTS = {
    "ru": """Ты — аудитор долгосрочной памяти. Проведи ревизию знаний и инструментов в домене "{domain}".
Текущая дата: {now}. Проанализируй:

1. УСТАРЕВШИЕ ЗНАНИЯ: записи, не подтверждённые > {staleness_days} дней (сравни created_at с {now}).
2. ПРОТИВОРЕЧИЯ ВНУТРИ L3: два правила конфликтуют друг с другом.
3. МЁРТВЫЕ ИНСТРУМЕНТЫ: инструменты без обращений > {unused_days} дней.
4. ДУБЛИКАТЫ ЗНАНИЙ: одно знание сохранено дважды в разных формулировках.
5. ОЦЕНКА ЗДОРОВЬЯ: процент неиспользуемого, доля противоречий, рекомендации.

ВАЖНО: НЕ помечай как устаревшие записи, созданные менее {staleness_days} дней назад. Сравнивай created_at с {now}.
Для каждой проблемы укажи ID и рекомендуемое действие.
Ответь JSON:
{{"stale_knowledge_ids": [...], "internal_conflicts": [...], "dead_tool_ids": [...], "duplicate_pairs": [...], "health_score": 0.85, "recommendations": "..."}}""",

    "en": """You are a long-term memory auditor. Audit knowledge and tools in the "{domain}" domain.
Current date: {now}. Analyze:

1. STALE KNOWLEDGE: records unconfirmed for > {staleness_days} days (compare created_at with {now}).
2. INTERNAL L3 CONFLICTS: two rules contradicting each other.
3. DEAD TOOLS: tools unused for > {unused_days} days.
4. KNOWLEDGE DUPLICATES: same knowledge stored twice in different wording.
5. HEALTH SCORE: percentage of unused, conflict rate, recommendations.

IMPORTANT: Do NOT mark as stale records created less than {staleness_days} days ago. Compare created_at with {now}.
For each issue, specify the ID and recommended action.
Respond in JSON:
{{"stale_knowledge_ids": [...], "internal_conflicts": [...], "dead_tool_ids": [...], "duplicate_pairs": [...], "health_score": 0.85, "recommendations": "..."}}""",

    "zh": """你是长期记忆审计员。审计 "{domain}" 域的知识和工具。
当前日期: {now}。分析：过时知识、L3内部冲突、废弃工具、知识重复、健康评分。
重要：不要标记创建不足 {staleness_days} 天的记录为过时。比较 created_at 与 {now}。
以JSON回复：...""",

    "ja": """あなたは長期記憶監査人です。"{domain}" ドメインの知識とツールを監査してください。
現在日: {now}。分析：古い知識、L3内部矛盾、使われないツール、知識重複、健全性スコア。
重要：{staleness_days} 日未満のレコードを古いとマークしないでください。created_atと{now}を比較してください。
JSONで回答：...""",

    "ko": """당신은 장기 메모리 감사자입니다. "{domain}" 도메인의 지식과 도구를 감사하세요.
현재 날짜: {now}. 분석: 오래된 지식, L3 내부 충돌, 사용되지 않는 도구, 지식 중복, 건강 점수.
중요: {staleness_days}일 미만 된 레코드를 오래된 것으로 표시하지 마세요. created_at을 {now}와 비교하세요.
JSON으로 응답:...""",

    "es": """Eres un auditor de memoria a largo plazo. Audita conocimiento y herramientas en "{domain}".
Fecha actual: {now}. Analiza: conocimiento obsoleto, conflictos L3, herramientas muertas, duplicados, puntuación de salud.
IMPORTANTE: NO marques como obsoletos registros creados hace menos de {staleness_days} días. Compara created_at con {now}.
Responde en JSON:...""",

    "ar": """أنت مدقق الذاكرة طويلة المدى. دقق المعرفة والأدوات في "{domain}".
التاريخ الحالي: {now}. حلل: المعرفة القديمة، تعارضات L3، الأدوات الميتة، تكرارات المعرفة، نقاط الصحة.
مهم: لا تضع علامة "قديم" على السجلات التي أنشئت قبل أقل من {staleness_days} يومًا. قارن created_at بـ {now}.
أجب بصيغة JSON:...""",

    "pt": """Você é um auditor de memória de longo prazo. Audite conhecimento e ferramentas em "{domain}".
Data atual: {now}. Analise: conhecimento obsoleto, conflitos L3, ferramentas mortas, duplicatas, pontuação de saúde.
IMPORTANTE: NÃO marque como obsoletos registros criados há menos de {staleness_days} dias. Compare created_at com {now}.
Responda em JSON:...""",
}

# ==================== PUBLIC API ====================

def get_daily_prompt(language: str | None = None) -> str:
    return DAILY_PROMPTS.get(language or lang(), DAILY_PROMPTS["ru"])

def get_weekly_prompt(language: str | None = None) -> str:
    return WEEKLY_PROMPTS.get(language or lang(), WEEKLY_PROMPTS["ru"])

def get_filter_prompt(language: str | None = None) -> str:
    return FILTER_PROMPTS.get(language or lang(), FILTER_PROMPTS["ru"])

def get_quality_prompt(language: str | None = None) -> str:
    return QUALITY_PROMPTS.get(language or lang(), QUALITY_PROMPTS["ru"])

def get_audit_prompt(language: str | None = None) -> str:
    return AUDIT_PROMPTS.get(language or lang(), AUDIT_PROMPTS["ru"])

def get_user_daily(language: str | None = None) -> str:
    return USER_DAILY.get(language or lang(), USER_DAILY["ru"])

def get_user_weekly(language: str | None = None) -> str:
    return USER_WEEKLY.get(language or lang(), USER_WEEKLY["ru"])

def get_user_retry(language: str | None = None) -> str:
    return USER_RETRY.get(language or lang(), USER_RETRY["ru"])

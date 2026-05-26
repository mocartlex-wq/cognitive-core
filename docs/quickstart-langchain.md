# Quickstart: LangChain + Cognitive Core за 10 минут

## Что получится

LangChain агент с **persistent memory** между запусками: вместо ConversationBufferMemory (теряется при рестарте) — память хранится на сервере, доступна с любой машины, индексируется семантически (KNN).

Cognitive Core работает как `BaseMemory` через HTTP API, ничего ставить дополнительно не нужно — только `requests` или `httpx`.

## Шаги

### 1. Зарегистрируйтесь
- https://mcp.me-ai.ru/ui/pricing → «Начать бесплатно»
- Email → OTP-код → готово

### 2. Получите API key
В профиле → «Мои помощники» → «➕ Создать помощника»:
- Имя: `langchain-bot` (или своё)
- Тип: «Generic API»
- Скопируйте сгенерированный `api_key` (показывается ОДИН РАЗ)

### 3. Установите зависимости
```bash
pip install langchain langchain-openai requests
```

### 4. Создайте memory-класс

```python
# cognitive_memory.py
import requests
from typing import Any
from langchain.memory.chat_memory import BaseChatMemory
from langchain.schema import HumanMessage, AIMessage


API_BASE = "https://mcp.me-ai.ru"
API_KEY = "<paste-your-api_key-here>"


class CognitiveMemory(BaseChatMemory):
    """LangChain memory backed by Cognitive Core (server-side persistent memory)."""

    domain: str = "langchain_session"
    memory_key: str = "history"

    def _save(self, role: str, content: str) -> None:
        requests.post(
            f"{API_BASE}/events",
            headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
            json={
                "domain": self.domain,
                "payload": {"role": role, "content": content},
            },
            timeout=10,
        ).raise_for_status()

    def _recall(self, query: str, limit: int = 20) -> list[dict]:
        r = requests.post(
            f"{API_BASE}/mcp/messages",
            headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
            json={
                "jsonrpc": "2.0", "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "cognitive_recall",
                    "arguments": {"query": query, "domain": self.domain, "limit": limit},
                },
            },
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("result", {}).get("structuredContent", {}).get("hits", [])

    def save_context(self, inputs: dict[str, Any], outputs: dict[str, str]) -> None:
        user_msg = inputs.get("input", "")
        ai_msg = outputs.get("output", "")
        self._save("user", user_msg)
        self._save("assistant", ai_msg)

    def load_memory_variables(self, inputs: dict[str, Any]) -> dict[str, Any]:
        query = inputs.get("input", "")
        hits = self._recall(query, limit=10)
        messages = []
        for h in hits:
            payload = h.get("payload", {})
            role = payload.get("role", "user")
            content = payload.get("content", "")
            if role == "user":
                messages.append(HumanMessage(content=content))
            else:
                messages.append(AIMessage(content=content))
        return {self.memory_key: messages}

    def clear(self) -> None:
        # Удаление — отдельный admin endpoint, по умолчанию не вызывается
        pass
```

### 5. Подключите к chain

```python
# example.py
from langchain.chains import ConversationChain
from langchain_openai import ChatOpenAI
from cognitive_memory import CognitiveMemory

llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.7)
memory = CognitiveMemory(domain="my_project")

conv = ConversationChain(llm=llm, memory=memory, verbose=False)

# Запуск 1
print(conv.predict(input="Привет, я работаю над проектом A для клиента Иван"))

# Запуск 2 (другой Python процесс, через час, через день — память сохранится)
print(conv.predict(input="Над каким проектом я работаю?"))
# → "Вы работаете над проектом A для клиента Иван"
```

### 6. Проверьте
- Откройте https://mcp.me-ai.ru/ui/profile → «История событий» — увидите свои сохранённые турны
- Запустите второй процесс с тем же `domain` — он унаследует ту же память
- `cognitive_domains()` через MCP покажет какие домены вы создали

## Best practices

1. **Один `domain` = один логический проект.** Не пишите всё в `default` — recall будет шумным.
2. **Не пишите PII в payload** (см. [SECURITY.md](../SECURITY.md)) — используйте ссылку на ваш external user_id.
3. **При длинных диалогах** — раз в 50 турнов вызывайте `cognitive_consolidate` для перевода L1 → L2 (это происходит и автоматически раз в сутки).
4. **Тестируйте локально:** `requests` ≠ async — для production используйте `httpx.AsyncClient` + LangChain async chains.

## Альтернативный путь: MCP-bridge

Если используете LangChain >= 0.2 с MCP-поддержкой — можно подключить cognitive-core как remote MCP server напрямую:

```python
from langchain_mcp import MultiServerMCPClient

client = MultiServerMCPClient({
    "cognitive-core": {
        "url": "https://mcp.me-ai.ru/mcp/sse",
        "headers": {"X-API-Key": API_KEY},
    },
})
tools = await client.get_tools()  # все 25 MCP инструментов
```

## Quota

Free tier: 10 000 событий/день. При большой нагрузке (1000+ турнов/час) — upgrade на Pro в /ui/pricing.

## Поддержка
- Email: support@me-ai.ru
- Документация: https://mcp.me-ai.ru/docs/concepts.md
- Список tools: https://mcp.me-ai.ru/api/openapi/cognitive.yaml

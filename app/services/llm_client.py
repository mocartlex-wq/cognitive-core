import json
import random
import time as _time
from openai import AsyncOpenAI
from app.config import settings

_response_cache: dict[str, dict[str, str]] = {}
_ab_stats: dict[str, dict[str, int]] = {}  # {function: {"a_success": N, "b_success": N, "a_fail": N, "b_fail": N}}


# ─── Circuit Breaker ──────────────────────────────────────────────────────────
# Per-endpoint (base_url:model) state machine. Защищает от висения на dead
# endpoint: после N последовательных fail переходит в OPEN и fail-fast'ит
# на T секунд, потом HALF_OPEN — один пробный вызов; success → CLOSED,
# fail → opnt OPEN. Не блокирует chain — если primary OPEN, fallback всё равно
# попробуется (его breaker отдельный).

class CircuitState:
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(self, threshold: int, timeout: int):
        self.threshold = threshold
        self.timeout = timeout
        self.failures = 0
        self.state = CircuitState.CLOSED
        self.opened_at = 0.0

    def allow(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if _time.monotonic() - self.opened_at > self.timeout:
                self.state = CircuitState.HALF_OPEN
                return True
            return False
        # HALF_OPEN — let one probe through (next call will trigger record_*)
        return True

    def record_success(self) -> None:
        self.failures = 0
        self.state = CircuitState.CLOSED

    def record_failure(self) -> None:
        self.failures += 1
        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.OPEN
            self.opened_at = _time.monotonic()
        elif self.failures >= self.threshold:
            self.state = CircuitState.OPEN
            self.opened_at = _time.monotonic()


_breakers: dict[str, CircuitBreaker] = {}


def _get_breaker(endpoint_key: str) -> CircuitBreaker:
    if endpoint_key not in _breakers:
        _breakers[endpoint_key] = CircuitBreaker(
            threshold=settings.llm_circuit_threshold,
            timeout=settings.llm_circuit_timeout,
        )
    return _breakers[endpoint_key]


def get_circuit_states() -> dict:
    """Snapshot circuit-breaker state for /health observability."""
    return {
        key: {
            "state": b.state,
            "failures": b.failures,
            "opened_seconds_ago": round(_time.monotonic() - b.opened_at, 1) if b.state != CircuitState.CLOSED else None,
        }
        for key, b in _breakers.items()
    }


def reset_circuit_breakers() -> None:
    """For testing / manual recovery."""
    _breakers.clear()


def _build_client(base_url: str, api_key: str) -> AsyncOpenAI:
    return AsyncOpenAI(base_url=base_url, api_key=api_key)


class LLMClient:
    """Единый клиент с поддержкой: DeepSeek / OpenAI / Ollama + A/B-тестирование."""

    def __init__(self, function_name: str):
        self.function_name = function_name
        model_name = getattr(settings, f"llm_{function_name}", settings.llm_fallback)

        # Основная модель
        self.primary_config = settings.get_model_config(model_name)
        self.primary_model = model_name

        # A/B тестирование — support for all functions
        self.ab_model: str = ""
        self.ab_config: dict = {}
        ab_model_name = getattr(settings, f"llm_{function_name}_b", "")
        if ab_model_name and settings.llm_ab_traffic_percent > 0:
            try:
                self.ab_config = settings.get_model_config(ab_model_name)
                self.ab_model = ab_model_name
            except ValueError:
                pass

        # Fallback
        self.fallback_config = settings.get_model_config(settings.llm_fallback)
        self.fallback_model = settings.llm_fallback

        # Ollama
        self.ollama_config = {}
        self.ollama_model = ""
        if settings.local_ai_enabled:
            self.ollama_config = {
                "base_url": f"{settings.ollama_base_url}/v1",
                "api_key": "ollama",
                "model": settings.ollama_curator_model,
            }
            self.ollama_model = settings.ollama_curator_model

        self.temperature = settings.curator_temperature if ("curator" in function_name or "audit" in function_name) else 0.3

    async def call(self, system_prompt: str, user_prompt: str, domain: str = "default") -> dict:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        # Определяем модель для этого вызова
        config, model = self._pick_model()
        is_ab = bool(self.ab_model and model == self.ab_model)

        # Цепочка попыток
        chain = [(config, model)]
        if config is not self.primary_config:
            chain.append((self.primary_config, self.primary_model))
        chain.append((self.fallback_config, self.fallback_model))
        if self.ollama_config and self.ollama_config not in [c for c, _ in chain]:
            chain.append((self.ollama_config, self.ollama_model))
        # Повтор primary если была A/B
        if config is not self.primary_config:
            chain.insert(1, (self.primary_config, self.primary_model))

        tried = set()
        skipped_open = []  # endpoints we skipped due to OPEN circuit (for log)
        for cfg, mdl in chain:
            key = f"{cfg['base_url']}:{mdl}"
            if key in tried:
                continue
            tried.add(key)

            # Circuit-breaker gate — skip endpoint if its breaker is OPEN
            breaker = _get_breaker(key)
            if not breaker.allow():
                skipped_open.append(key)
                continue

            try:
                start = _time.monotonic()
                result = await self._try_call(cfg, mdl, messages)
                duration = _time.monotonic() - start
                if result:
                    breaker.record_success()
                    self._cache_response(domain, result)
                    _track_ab(self.function_name, is_ab, True)
                    from app.services.metrics import track_llm
                    track_llm(self.function_name, mdl, "success", duration)
                    return result
            except Exception:
                breaker.record_failure()
                _track_ab(self.function_name, is_ab, False)

        # All fresh attempts exhausted — try last-known-good cached response.
        # Graceful degradation: caller gets stale-but-valid data instead of crash.
        cached = self._get_cached(domain)
        if cached:
            from app.services.metrics import log_event
            log_event(
                "warning",
                "all LLM endpoints unavailable, returning cached response",
                function=self.function_name, domain=domain,
                tried=list(tried), skipped_open=skipped_open,
            )
            return cached

        raise RuntimeError(
            f"All LLM calls failed for function '{self.function_name}' "
            f"(tried={len(tried)}, skipped_open={len(skipped_open)}, no cache)"
        )

    async def embed(self, text: str) -> list[float]:
        config = settings.get_model_config(settings.llm_embedding)
        client = AsyncOpenAI(base_url=config["base_url"], api_key=config["api_key"])
        response = await client.with_options(timeout=12.0).embeddings.create(model=config["model"], input=text)
        return response.data[0].embedding

    def _pick_model(self) -> tuple[dict, str]:
        """Выбирает модель: A/B сплит если активирован."""
        if self.ab_model and settings.llm_ab_traffic_percent > 0:
            if random.randint(1, 100) <= settings.llm_ab_traffic_percent:
                return self.ab_config, self.ab_model
        return self.primary_config, self.primary_model

    async def _try_call(self, config: dict, model: str, messages: list) -> dict | None:
        client = AsyncOpenAI(base_url=config["base_url"], api_key=config["api_key"])
        response = await client.with_options(timeout=20.0).chat.completions.create(
            model=model,
            messages=messages,
            temperature=self.temperature,
            max_tokens=4096,
        )
        text = response.choices[0].message.content
        if not text:
            return None
        return self._parse_json(text)

    @staticmethod
    def _parse_json(text: str) -> dict:
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        start = text.find("{")
        if start == -1:
            start = text.find("[")
        end = text.rfind("}")
        if end == -1:
            end = text.rfind("]")
        if start >= 0 and end > start:
            text = text[start:end + 1]
        return json.loads(text)

    def _cache_response(self, domain: str, data: dict) -> None:
        key = f"{self.function_name}:{domain}"
        if self.function_name not in _response_cache:
            _response_cache[self.function_name] = {}
        _response_cache[self.function_name][key] = json.dumps(data)

    def _get_cached(self, domain: str) -> dict | None:
        key = f"{self.function_name}:{domain}"
        cached = _response_cache.get(self.function_name, {}).get(key)
        if cached:
            return json.loads(cached)
        return None


def _track_ab(function: str, is_b: bool, success: bool):
    """Обновляет A/B статистику."""
    if function not in _ab_stats:
        _ab_stats[function] = {"a_success": 0, "b_success": 0, "a_fail": 0, "b_fail": 0}
    variant = "b" if is_b else "a"
    outcome = "success" if success else "fail"
    _ab_stats[function][f"{variant}_{outcome}"] += 1


def get_ab_stats() -> dict:
    """Возвращает A/B статистику по всем функциям."""
    result = {}
    for func, stats in _ab_stats.items():
        a_total = stats["a_success"] + stats["a_fail"]
        b_total = stats["b_success"] + stats["b_fail"]
        result[func] = {
            **stats,
            "a_total": a_total,
            "b_total": b_total,
            "a_success_rate": round(stats["a_success"] / a_total, 3) if a_total > 0 else None,
            "b_success_rate": round(stats["b_success"] / b_total, 3) if b_total > 0 else None,
        }
    return result


def get_llm_client(function: str) -> LLMClient:
    return LLMClient(function)

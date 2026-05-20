"""Settings cognitive-core.

Расширение 2026-05-17: добавлены параметры email + аккаунты для Фазы 1A/2.
Все новые поля имеют дефолты — старые .env-файлы продолжают работать без изменений.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict
import json


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    # Хранилища
    database_url: str = "postgresql://cognitive:cognitive_secret@localhost:5432/cognitive_core"
    redis_url: str = "redis://localhost:6379"
    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "l4-snapshots"
    s3_secure: bool = False

    # Модельный реестр
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_api_key: str = ""
    llm_daily_analyzer: str = "deepseek-chat"
    llm_weekly_consolidator: str = "deepseek-chat"
    llm_curator_filter: str = "deepseek-chat"
    llm_curator_quality: str = "deepseek-chat"
    llm_curator_audit: str = "deepseek-chat"
    llm_curator_arbitration: str = "deepseek-chat"
    llm_embedding: str = "deepseek-embedding"

    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    llm_fallback: str = "gpt-4o-mini"

    # A/B тестирование (работает для любой функции: llm_<function>_b)
    llm_daily_analyzer_b: str = ""
    llm_weekly_consolidator_b: str = ""
    llm_curator_filter_b: str = ""
    llm_curator_quality_b: str = ""
    llm_curator_audit_b: str = ""
    llm_curator_arbitration_b: str = ""
    llm_ab_traffic_percent: int = 0

    # Локальный ИИ
    local_ai_enabled: bool = False
    ollama_base_url: str = "http://ollama:11434"
    ollama_curator_model: str = "qwen3:14b"
    ollama_fast_model: str = "llama4:8b"
    ollama_embedding_model: str = "nomic-embed-text"

    # Безопасность
    agent_api_keys: str = '{"agent_default":"default-key"}'
    max_payload_size: int = 262144  # 256KB
    rate_limit_per_agent: int = 100
    max_payload_depth: int = 10
    max_payload_keys: int = 500

    # TLS
    ssl_cert_path: str = ""
    ssl_key_path: str = ""

    # Куратор
    curator_temperature: float = 0.1
    min_events_for_daily: int = 3
    min_confidence_for_l3: float = 0.6
    min_l2_repetitions_for_l3: int = 2
    l4_full_snapshot_interval_weeks: int = 4
    l4_min_change_percent: float = 5.0
    l3_staleness_days: int = 90
    tool_unused_days: int = 60

    # Циклы
    retention_days: int = 14
    daily_hours: int = 24
    weekly_days: int = 7

    # LLM circuit breaker (per provider+model)
    llm_circuit_threshold: int = 3   # consecutive failures to trip OPEN
    llm_circuit_timeout: int = 60    # seconds in OPEN before half-open probe

    # Язык
    system_language: str = "ru"  # ru / en / zh

    # ─────────────────────────────────────────────────────────────────────
    # EMAIL + АККАУНТЫ (добавлено 2026-05-17, Фаза 1A/2)
    # ─────────────────────────────────────────────────────────────────────
    # Бекенд для отправки писем: "yandex" (smtp.yandex.ru), "postfix" (свой),
    # "stdout" (для dev — печатает письмо в лог вместо отправки).
    email_backend: str = "stdout"

    smtp_host: str = "smtp.yandex.ru"
    smtp_port: int = 465
    smtp_user: str = ""           # для yandex — mozartlex@yandex.ru
    smtp_password: str = ""       # для yandex — APP-PASSWORD (не обычный пароль!)

    email_from: str = "mozartlex@yandex.ru"
    email_from_name: str = "AImail"
    email_reply_to: str = "noreply@aimail.art"

    # Корневой URL приложения — используется при формировании magic-link
    # и редиректа после входа. На сервере: https://mcp.xn----8sbwawqx4fza.xn--p1ai
    # или https://aimail.art когда DNS заработает.
    app_url: str = "https://aimail.art"
    magic_link_ttl_minutes: int = 15

    # Email владельца-bootstrap. При первом входе под этим адресом
    # пользователь получает is_admin=TRUE + все legacy комнаты/помощники
    # привязываются к его user_id. Пусто = bootstrap не применяется.
    owner_bootstrap_email: str = ""

    # Пароль bootstrap-владельца. Если задан И email совпадает с
    # owner_bootstrap_email — позволяет войти по паролю через
    # POST /auth/password/login без предварительного magic-link.
    # На первом успешном входе пароль захешируется и сохранится в
    # accounts.password_hash. После этого env-переменную можно очистить.
    owner_bootstrap_password: str = ""

    # ─────────────────────────────────────────────────────────────────────
    # Логирование (Phase 3 — секреты редактируются автоматически)
    # ─────────────────────────────────────────────────────────────────────
    # Если True, middleware маскирует значения чувствительных заголовков
    # и query-параметров в access-логе.
    log_redact_secrets: bool = True

    def get_agent_keys(self) -> dict:
        return json.loads(self.agent_api_keys)

    def get_model_config(self, model_name: str) -> dict:
        """Возвращает конфиг для конкретной модели."""
        if model_name.startswith("deepseek"):
            return {
                "base_url": self.deepseek_base_url,
                "api_key": self.deepseek_api_key,
                "model": model_name,
            }
        elif model_name.startswith("gpt") or model_name.startswith("o1"):
            return {
                "base_url": self.openai_base_url,
                "api_key": self.openai_api_key,
                "model": model_name,
            }
        elif self.local_ai_enabled and model_name.startswith("ollama:"):
            return {
                "base_url": f"{self.ollama_base_url}/v1",
                "api_key": "ollama",
                "model": model_name.replace("ollama:", ""),
            }
        else:
            raise ValueError(f"Unknown model: {model_name}")


settings = Settings()

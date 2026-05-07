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

    # Язык
    system_language: str = "ru"  # ru / en / zh

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


from pydantic import BaseModel, ConfigDict, field_validator

VALID_TOOL_TYPES = {"api", "script", "prompt", "library", "service"}


class ToolRegistryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: str
    tool_name: str
    tool_type: str

    @field_validator("tool_type")
    @classmethod
    def validate_tool_type(cls, v: str) -> str:
        if v not in VALID_TOOL_TYPES:
            raise ValueError(f"tool_type must be one of {VALID_TOOL_TYPES}, got: {v}")
        return v
    description: str | None = None
    config_schema: dict | None = None
    usage_patterns: dict | None = None
    confidence: float = 0.5

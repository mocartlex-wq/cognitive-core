"""Immutable, bounded scopes for advisory ordering of two materials."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

Label = Annotated[str, Field(min_length=1, max_length=120)]
Order = Literal["diagram_first", "table_first"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Rule(Strict):
    address: str = Field(pattern=r"^(?:[1-9]|10)(?:\.[1-9][0-9]{0,3}){0,4}$")
    choice: Order
    weight: int = Field(ge=1, le=10, strict=True)
    reason: Label
    required: bool = True
    when: list[Label] = Field(default_factory=list, max_length=12)


class Case(Strict):
    title: Label
    project: Label
    team: Label
    role: Label
    action: Literal["order_demo_materials"] = "order_demo_materials"
    conditions: list[Label] = Field(min_length=1, max_length=12)
    agents: list[Label] = Field(min_length=1, max_length=32)
    rules: list[Rule] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def verify_tree(self):
        nodes = {r.address: r for r in self.rules}
        if len(nodes) != len(self.rules):
            raise ValueError("Адреса правил в ситуации должны различаться.")
        for rule in self.rules:
            if not set(rule.when) <= set(self.conditions):
                raise ValueError("Условие правила должно входить в заданную область.")
            parent_address, dot, _ = rule.address.rpartition(".")
            if dot:
                parent = nodes.get(parent_address)
                if parent is None or parent.weight != 10 or not set(parent.when) < set(rule.when) or parent.required != rule.required:
                    raise ValueError("Уточнение требует родителя веса 10 и более узкого условия той же обязательности.")
        return self


class Answer(Strict):
    choice: Order
    duration: Literal["once", "future"]


class Start(Strict):
    case_id: UUID
    instance_id: UUID
    chat_answer: Answer | None = Field(default=None, description="Вариант и срок, явно названные владельцем в чате. Готовит ссылку для подтверждения.")


class Feedback(Strict):
    id: UUID
    rule_id: UUID
    evidence_run_id: UUID
    delta: Literal[-1, 1]
    reason: Label

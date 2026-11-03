"""Validated task contracts shared by replay, users, grading and reporting."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

ActionName = Literal[
    "modify_address",
    "cancel_order",
    "apply_price_protection",
    "request_return",
    "request_exchange",
    "create_order",
    "submit_approval",
    "transfer_to_human",
]


class GoldAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: ActionName
    args: dict[str, JsonValue]


class UserProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone: str
    name: str


class AmountSource(BaseModel):
    """Select a Services result, never a manually supplied expected amount."""

    model_config = ConfigDict(extra="forbid")
    action_index: int = Field(ge=0)


class EvalTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,70}$")
    tags: list[str]
    user_profile: UserProfile
    opening_message: str = Field(min_length=1)
    instruction: str
    scripted_replies: list[str] = Field(min_length=1)
    max_turns: int = Field(default=6, ge=1, le=20)
    gold_actions: list[GoldAction]
    amount_sources: list[AmountSource] = Field(default_factory=list)
    must_mention: list[str] = Field(default_factory=list)
    must_not: list[str] = Field(default_factory=list)
    expect_handoff: bool = False

    @model_validator(mode="after")
    def validate_sources(self) -> "EvalTask":
        if any(source.action_index >= len(self.gold_actions) for source in self.amount_sources):
            raise ValueError("Amount source must reference a gold action")
        return self


def load_tasks(directory: str | Path) -> list[EvalTask]:
    tasks = [
        EvalTask.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        for path in sorted(Path(directory).glob("*.yaml"))
    ]
    if not tasks:
        raise ValueError("任务目录没有 YAML 任务")
    if len({task.id for task in tasks}) != len(tasks):
        raise ValueError("任务 ID 重复")
    return tasks

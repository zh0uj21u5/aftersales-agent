"""Explicit environment loading and shared harness configuration."""

import os
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import BaseModel, Field, SecretStr


class AgentConfig(BaseModel):
    model: str = "mock"
    provider: str = "mock"
    policy_mode: Literal["full", "on_demand"] = "full"
    guard: bool = True
    confirm_gate: bool = True
    confirm_mode: Literal["heuristic", "llm"] = "heuristic"
    todo: bool = False
    goal_judge: bool = False
    preview_tool: bool = True
    max_steps: int = Field(default=30, ge=1)
    max_tokens: int = Field(default=2048, ge=1)
    temperature: float = Field(default=0.0, ge=0)
    tool_output_max_chars: int = Field(default=8000, ge=64)


class LLMSettings(BaseModel):
    provider: str
    model: str
    api_key: SecretStr
    base_url: str | None = None
    max_steps: int = Field(default=30, ge=1)
    tool_output_max_chars: int = Field(default=8000, ge=64)

    def agent_config(self) -> AgentConfig:
        return AgentConfig(
            model=self.model,
            provider=self.provider,
            max_steps=self.max_steps,
            tool_output_max_chars=self.tool_output_max_chars,
        )


def load_settings(
    env_file: str | Path = ".env",
    *,
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> LLMSettings:
    """Load without process mutation; explicit values override environment."""
    values = {**dotenv_values(env_file), **os.environ}
    selected = (provider or values.get("LLM_PROVIDER") or "openai").strip().lower()
    if selected not in {"openai", "deepseek", "qwen", "glm", "anthropic"}:
        raise ValueError("不支持的 LLM_PROVIDER")
    prefix = "ANTHROPIC" if selected == "anthropic" else "OPENAI"
    key = (api_key or values.get(f"{prefix}_API_KEY") or "").strip()
    model_id = (model or values.get("MODEL_ID") or "").strip()
    if not key or not model_id:
        raise ValueError(f"请在 .env 配置 {prefix}_API_KEY 和 MODEL_ID")
    return LLMSettings(
        provider=selected,
        model=model_id,
        api_key=SecretStr(key),
        base_url=(base_url or values.get(f"{prefix}_BASE_URL") or "").strip() or None,
        max_steps=values.get("MAX_STEPS") or 30,
        tool_output_max_chars=values.get("TOOL_OUTPUT_MAX_CHARS") or 8000,
    )

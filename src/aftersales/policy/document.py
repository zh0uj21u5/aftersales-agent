"""Read policy sections by their explicit topic markers."""

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

PolicyTopic = Literal[
    "auth",
    "confirm",
    "address_cancel",
    "price_protection",
    "return",
    "refund",
    "exchange",
    "coupon",
    "approval",
    "security",
    "escalation",
]
POLICY_PATH = Path(__file__).with_name("POLICY.md")


@lru_cache(maxsize=4)
def _read_policy(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def policy_text() -> str:
    return _read_policy(POLICY_PATH)


def lookup_topic(topic: PolicyTopic) -> str:
    sections = re.split(r"(?=^## P\d+ )", policy_text(), flags=re.MULTILINE)
    selected = [section.strip() for section in sections if f"<!-- topic: {topic} -->" in section]
    if not selected:
        raise ValueError("未知的规则主题")
    return "\n\n".join(selected)

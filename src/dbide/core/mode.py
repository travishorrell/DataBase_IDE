"""The two ways to use the IDE.

learning  Learning mode: lessons, playground, SQL/concept explanations, safety prompts,
          tools revealed as skills grow. It teaches you to become a database admin.
power     Power user mode: every tool available at once, help only about the software
          itself (not SQL), no prompts. For working database admins.
"""
from __future__ import annotations

from dbide.core.settings import settings

LEARNING = "learning"
POWER = "power"
NAMES = {LEARNING: "Learning mode", POWER: "Power user mode"}


def current() -> str:
    m = settings().get("mode")
    if m in NAMES:
        return m
    # older settings: the beginner switch decided
    return LEARNING if settings().get("beginner_mode") is not False else POWER


def is_learning() -> bool:
    return current() == LEARNING


def is_power() -> bool:
    return current() == POWER


def set_mode(m: str) -> None:
    if m not in NAMES:
        raise ValueError(m)
    settings().set("mode", m)
    settings().set("beginner_mode", m == LEARNING)

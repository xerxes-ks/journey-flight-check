"""Finite, bounded, nonnegative money values. Zero is zero, not a missing default."""

from __future__ import annotations

import math
from typing import Any

from .exceptions import InvalidBudget
from .model import DEFAULT_BUDGET_USD, DEFAULT_COST_PER_CALL

MAX_BUDGET_USD = 50.0
MAX_DAILY_BUDGET_USD = 50.0
MAX_COST_PER_CALL = 1.0
HTTP_BUDGET_CAP = 0.50

_NON_FINITE_NAMES = {
    "nan", "+nan", "-nan",
    "inf", "+inf", "-inf",
    "infinity", "+infinity", "-infinity",
}


def parse_money(
    value: Any,
    default: float = DEFAULT_BUDGET_USD,
    *,
    max_value: float = MAX_BUDGET_USD,
    field: str = "budget",
) -> float:
    """Parse a money limit. Reject NaN/Infinity (and their string forms).

    ``None`` / ``""`` yield *default*. ``0`` is a valid cap of zero.
    Values above *max_value* are clamped (bounded). Negatives are rejected.
    """
    if value is None or value == "":
        return float(default)
    if isinstance(value, bool):
        raise InvalidBudget(f"{field} must be a finite number")
    if isinstance(value, str):
        text = value.strip()
        if text.lower() in _NON_FINITE_NAMES:
            raise InvalidBudget(f"{field} must be a finite number")
        try:
            number = float(text)
        except ValueError:
            raise InvalidBudget(f"{field} must be a finite number") from None
    elif isinstance(value, (int, float)):
        number = float(value)
    else:
        raise InvalidBudget(f"{field} must be a finite number")
    if not math.isfinite(number):
        raise InvalidBudget(f"{field} must be a finite number")
    if number < 0:
        raise InvalidBudget(f"{field} must be nonnegative")
    cap = float(max_value)
    if number > cap:
        return cap
    return number


def request_budget(
    data: dict,
    default: float = DEFAULT_BUDGET_USD,
    *,
    max_value: float = MAX_BUDGET_USD,
) -> float:
    """Read budget_usd or budget from a request mapping. Missing yields *default*."""
    if "budget_usd" in data:
        raw = data.get("budget_usd")
    elif "budget" in data:
        raw = data.get("budget")
    else:
        raw = None
    return parse_money(raw, default, max_value=max_value, field="budget")


def finite_unit_price(value: Any, default: float = DEFAULT_COST_PER_CALL) -> float:
    """Server-owned unit price: ignore non-finite or negative client values."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(number) or number < 0:
        return float(default)
    if number > MAX_COST_PER_CALL:
        return float(default)
    return number

from __future__ import annotations

import json
import re
from typing import Any


def parse_market_outcomes(raw_outcomes: Any) -> list[str]:
    """Parses market outcomes (list or JSON string) into normalized lowercase labels."""
    if isinstance(raw_outcomes, list):
        return [str(item).strip().lower() for item in raw_outcomes]
    if isinstance(raw_outcomes, str) and raw_outcomes:
        try:
            decoded = json.loads(raw_outcomes)
            if isinstance(decoded, list):
                return [str(item).strip().lower() for item in decoded]
        except json.JSONDecodeError:
            return []
    return []


def extract_market_price_boundaries(market: dict[str, Any]) -> list[float]:
    """Extracts numeric boundary values from a market's title/question fields."""
    text = str(market.get("groupItemTitle") or market.get("question") or "")
    values = [float(raw.replace(",", "")) for raw in re.findall(r"\d[\d,]*(?:\.\d+)?", text)]
    return [value for value in values if value > 0]


def extract_market_boundary_spec(
    market: dict[str, Any],
    event_title: str | None = None,
) -> tuple[str, list[float]]:
    """Returns (market_type, boundaries) where type is updown/range/upper/lower/unknown."""
    outcomes = parse_market_outcomes(market.get("outcomes"))
    if set(outcomes) == {"up", "down"}:
        group_item_title = str(market.get("groupItemTitle") or "")
        values = [float(raw.replace(",", "")) for raw in re.findall(r"\d[\d,]*(?:\.\d+)?", group_item_title)]
        boundaries = [value for value in values if value > 0]
        return "updown", boundaries[:1]

    group_item_title = str(market.get("groupItemTitle") or "")
    group_lower = group_item_title.strip().lower()
    market_question = str(market.get("question") or "").lower()
    event_title_lower = str(event_title or "").lower()
    context = f"{event_title_lower} {market_question}".strip()

    values = [float(raw.replace(",", "")) for raw in re.findall(r"\d[\d,]*(?:\.\d+)?", group_item_title)]
    values = [value for value in values if value > 0]

    if len(values) >= 2 and re.search(r"\d\s*[-–]\s*\d", group_item_title):
        lo, hi = sorted(values[:2])
        return "range", [lo, hi]

    if group_lower.startswith("<"):
        return "lower", values[:1]
    if group_lower.startswith(">"):
        return "upper", values[:1]
    if group_lower.startswith("↓"):
        return "lower", values[:1]
    if group_lower.startswith("↑"):
        return "upper", values[:1]

    if values:
        if "above" in context or "higher" in context:
            return "upper", values[:1]
        if "below" in context or "lower" in context:
            return "lower", values[:1]

    return "unknown", []

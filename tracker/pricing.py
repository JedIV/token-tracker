"""Per-model price lookup; computes USD cost for one message."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

PRICES_PATH = Path(__file__).resolve().parent.parent / "prices.json"


@lru_cache(maxsize=1)
def _prices() -> dict:
    with open(PRICES_PATH) as f:
        return json.load(f)


def reload() -> None:
    _prices.cache_clear()


def _lookup(tool: str, model: str | None) -> dict:
    table = _prices().get(tool, {})
    if not model:
        return table.get("_default", {"input": 0, "output": 0, "cache_read": 0})
    if model in table:
        return table[model]

    # Variant suffixes — "-mini" should never fall back to a full-size base price.
    is_mini = "-mini" in model
    keys = sorted([k for k in table if not k.startswith("_")], key=len, reverse=True)

    # 1) longest prefix match, preferring keys that share the mini-ness.
    for k in keys:
        if model.startswith(k) and (is_mini == ("mini" in k)):
            return table[k]
    # 2) relax the mini constraint only if no mini variant exists in the table.
    if is_mini and not any("mini" in k for k in keys):
        for k in keys:
            if model.startswith(k):
                return table[k]
    if not is_mini:
        for k in keys:
            if model.startswith(k) and "mini" not in k:
                return table[k]

    return table.get("_default", {"input": 0, "output": 0, "cache_read": 0})


def cost_usd(
    tool: str,
    model: str | None,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read: int = 0,
    cache_write_5m: int = 0,
    cache_write_1h: int = 0,
) -> float:
    p = _lookup(tool, model)
    total = (
        input_tokens   * p.get("input", 0)
        + output_tokens * p.get("output", 0)
        + cache_read    * p.get("cache_read", 0)
        + cache_write_5m * p.get("cache_write_5m", 0)
        + cache_write_1h * p.get("cache_write_1h", 0)
    ) / 1_000_000
    return round(total, 6)

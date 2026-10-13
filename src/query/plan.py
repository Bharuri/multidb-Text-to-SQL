"""Stage 5: read the LLM reply as one plan object, or as a cannot_answer reply. Only the shape is checked here. Names and values are checked by the matcher."""

from __future__ import annotations

import json
import re
from typing import Any

from src.query.shapes import CALCULATION_TYPES

PLAN_KEYS = frozenset(
    {"table", "select", "dimensions", "metrics", "filters", "group_by", "having", "order_by", "calculations", "limit"}
)
METRIC_KEYS = frozenset({"function", "column", "alias"})
SHARE_KEYS = METRIC_KEYS | {"where"}
FILTER_KEYS = frozenset({"column", "operator", "value"})
HAVING_KEYS = frozenset({"metric", "operator", "value"})
ORDER_KEYS = frozenset({"ref", "direction"})
CALCULATION_KEYS = frozenset({"type", "metric", "date_column", "grain", "year"})
REPEAT_KEYS = frozenset({"customer", "date", "dimension"})
# repeat_purchase may be left out of a reply. The other plan keys are always required.
OPTIONAL_PLAN_KEYS = frozenset({"repeat_purchase"})
# Optional lists may be null or empty. The matcher treats both as "not used".
OPTIONAL_LIST_KEYS = ("dimensions", "metrics", "group_by", "having", "order_by", "calculations")
CANNOT_ANSWER_KEYS = frozenset({"cannot_answer", "reason"})
FENCE = re.compile(r"\A```(?:json)?[ \t]*\n(.*)\n```\Z", re.DOTALL)


class PlanError(Exception):
    """The reply is not a plan. `reasons` lists every problem found."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = tuple(reasons)


class CannotAnswer(Exception):
    """The LLM says the question cannot be answered from the metadata."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def parse_reply(raw: str) -> dict[str, Any]:
    """Return the plan dict. Raise CannotAnswer for a cannot_answer reply and PlanError for anything else invalid."""
    body = _strip_fence(raw.strip())
    try:
        parsed = json.loads(body, object_pairs_hook=_no_duplicate_keys)
    except _DuplicateKeyError as error:
        raise PlanError([f"duplicate key in reply: {error}"]) from None
    except json.JSONDecodeError as error:
        raise PlanError([f"reply is not one JSON object: {error.msg}"]) from None

    if not isinstance(parsed, dict):
        raise PlanError(["reply must be one JSON object"])
    if "cannot_answer" in parsed:
        return _cannot_answer(parsed)
    return _plan(parsed)


class _DuplicateKeyError(ValueError):
    pass


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def _strip_fence(text: str) -> str:
    """A code fence may wrap the whole reply. Text outside the fence is left in place, so it fails the JSON parse."""
    match = FENCE.match(text)
    return match.group(1) if match else text


def _cannot_answer(parsed: dict[str, Any]) -> dict[str, Any]:
    problems = _key_problems(parsed, CANNOT_ANSWER_KEYS, "reply")
    if parsed.get("cannot_answer") is not True:
        problems.append("cannot_answer must be true")
    reason = parsed.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        problems.append("reason must be non-empty text")
    if problems:
        raise PlanError(problems)
    raise CannotAnswer(reason)


def _plan(parsed: dict[str, Any]) -> dict[str, Any]:
    problems = _key_problems(parsed, PLAN_KEYS, "plan", OPTIONAL_PLAN_KEYS)

    if not isinstance(parsed.get("table"), str):
        problems.append("table must be text")

    select = parsed.get("select")
    if not isinstance(select, list) or not all(isinstance(item, str) for item in select):
        problems.append("select must be a list of column names")

    for key in OPTIONAL_LIST_KEYS:
        problems.extend(_list_problems(parsed.get(key), key))

    problems.extend(_items_problems(parsed.get("metrics"), "metrics", _metric_problems))

    filters = parsed.get("filters")
    if not isinstance(filters, list):
        problems.append("filters must be a list")
    else:
        for index, item in enumerate(filters):
            problems.extend(_filter_problems(item, f"filters[{index}]"))

    problems.extend(_items_problems(parsed.get("having"), "having", _having_problems))
    problems.extend(_items_problems(parsed.get("order_by"), "order_by", _order_problems))
    problems.extend(_items_problems(parsed.get("calculations"), "calculations", _calculation_problems))

    limit = parsed.get("limit")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int)):
        problems.append("limit must be a whole number or null")
    problems.extend(_repeat_problems(parsed.get("repeat_purchase")))

    if problems:
        raise PlanError(problems)
    plan = {key: parsed[key] for key in PLAN_KEYS}
    if "repeat_purchase" in parsed:
        plan["repeat_purchase"] = parsed["repeat_purchase"]
    return plan


def _key_problems(parsed: dict[str, Any], expected: frozenset[str], label: str, optional: frozenset[str] = frozenset()) -> list[str]:
    problems: list[str] = []
    missing = sorted(expected - parsed.keys())
    extra = sorted(parsed.keys() - expected - optional)
    if missing:
        problems.append(f"{label} is missing keys: {', '.join(missing)}")
    if extra:
        problems.append(f"{label} has keys that are not allowed: {', '.join(extra)}")
    return problems


def _repeat_problems(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return ["repeat_purchase must be an object with customer, date and dimension, or null"]
    problems = [f"repeat_purchase: {problem}" for problem in _key_problems(raw, REPEAT_KEYS, "repeat_purchase")]
    for field in ("customer", "date"):
        if not isinstance(raw.get(field), str):
            problems.append(f"repeat_purchase: {field} must be text")
    if raw.get("dimension") is not None and not isinstance(raw.get("dimension"), str):
        problems.append("repeat_purchase: dimension must be text or null")
    return problems


def _list_problems(raw: Any, key: str) -> list[str]:
    if raw is None or isinstance(raw, list):
        return []
    return [f"{key} must be a list or null"]


def _items_problems(raw: Any, key: str, check: Any) -> list[str]:
    """Check each object in an optional list with the given per-item check."""
    if not isinstance(raw, list):
        return []
    problems: list[str] = []
    for index, item in enumerate(raw):
        problems.extend(check(item, f"{key}[{index}]"))
    return problems


def _metric_problems(raw: Any, label: str) -> list[str]:
    if not isinstance(raw, dict):
        return [f"{label} must be an object with function, column and alias"]
    is_share = isinstance(raw.get("function"), str) and raw["function"].upper() == "SHARE"
    problems = [f"{label}: {problem}" for problem in _key_problems(raw, SHARE_KEYS if is_share else METRIC_KEYS, "metric")]
    for field in ("function", "column", "alias"):
        if not isinstance(raw.get(field), str):
            problems.append(f"{label}: {field} must be text")
    if is_share:
        where = raw.get("where")
        if not isinstance(where, list) or not where:
            problems.append(f"{label}: where must be a non-empty list of filters")
        else:
            for index, item in enumerate(where):
                problems.extend(_filter_problems(item, f"{label}.where[{index}]"))
    return problems


def _having_problems(raw: Any, label: str) -> list[str]:
    if not isinstance(raw, dict):
        return [f"{label} must be an object with metric, operator and value"]
    problems = [f"{label}: {problem}" for problem in _key_problems(raw, HAVING_KEYS, "having")]
    for field in ("metric", "operator"):
        if not isinstance(raw.get(field), str):
            problems.append(f"{label}: {field} must be text")
    return problems


def _order_problems(raw: Any, label: str) -> list[str]:
    if not isinstance(raw, dict):
        return [f"{label} must be an object with ref and direction"]
    problems = [f"{label}: {problem}" for problem in _key_problems(raw, ORDER_KEYS, "order_by")]
    for field in ("ref", "direction"):
        if not isinstance(raw.get(field), str):
            problems.append(f"{label}: {field} must be text")
    return problems


def _calculation_problems(raw: Any, label: str) -> list[str]:
    if not isinstance(raw, dict):
        return [f"{label} must be an object with type, metric, date_column, grain and year"]
    problems = [f"{label}: {problem}" for problem in _key_problems(raw, CALCULATION_KEYS, "calculation")]
    kind = raw.get("type")
    if isinstance(kind, str) and kind not in CALCULATION_TYPES:
        problems.append(f"{label}: calculation type {kind!r} is not supported. Use one of: {', '.join(CALCULATION_TYPES)}")
    elif not isinstance(kind, str):
        problems.append(f"{label}: type must be text")
    for field in ("metric", "date_column", "grain"):
        if not isinstance(raw.get(field), str):
            problems.append(f"{label}: {field} must be text")
    year = raw.get("year")
    if isinstance(year, bool) or not isinstance(year, int):
        problems.append(f"{label}: year must be a whole number")
    return problems


def _filter_problems(raw: Any, label: str) -> list[str]:
    if not isinstance(raw, dict):
        return [f"{label} must be an object with column, operator and value"]
    problems = [f"{label}: {problem}" for problem in _key_problems(raw, FILTER_KEYS, "filter")]
    if not isinstance(raw.get("column"), str):
        problems.append(f"{label}: column must be text")
    if not isinstance(raw.get("operator"), str):
        problems.append(f"{label}: operator must be text")
    return problems

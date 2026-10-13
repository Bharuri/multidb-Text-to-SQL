"""Stage 5: read the LLM reply as one plan object, or as a cannot_answer reply. Only the shape is checked here. Names and values are checked by the matcher."""

from __future__ import annotations

import json
import re
from typing import Any

PLAN_KEYS = frozenset({"table", "select", "aggregation", "filters", "limit"})
AGGREGATION_KEYS = frozenset({"function", "column"})
FILTER_KEYS = frozenset({"column", "operator", "value"})
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
    problems = _key_problems(parsed, PLAN_KEYS, "plan")

    if not isinstance(parsed.get("table"), str):
        problems.append("table must be text")

    select = parsed.get("select")
    if not isinstance(select, list) or not all(isinstance(item, str) for item in select):
        problems.append("select must be a list of column names")

    aggregation = parsed.get("aggregation")
    if aggregation is not None:
        problems.extend(_aggregation_problems(aggregation))

    filters = parsed.get("filters")
    if not isinstance(filters, list):
        problems.append("filters must be a list")
    else:
        for index, item in enumerate(filters):
            problems.extend(_filter_problems(item, index))

    limit = parsed.get("limit")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int)):
        problems.append("limit must be a whole number or null")

    if problems:
        raise PlanError(problems)
    return {key: parsed[key] for key in PLAN_KEYS}


def _key_problems(parsed: dict[str, Any], expected: frozenset[str], label: str) -> list[str]:
    problems: list[str] = []
    missing = sorted(expected - parsed.keys())
    extra = sorted(parsed.keys() - expected)
    if missing:
        problems.append(f"{label} is missing keys: {', '.join(missing)}")
    if extra:
        problems.append(f"{label} has keys that are not allowed: {', '.join(extra)}")
    return problems


def _aggregation_problems(raw: Any) -> list[str]:
    if not isinstance(raw, dict):
        return ["aggregation must be null or an object with function and column"]
    problems = _key_problems(raw, AGGREGATION_KEYS, "aggregation")
    if not isinstance(raw.get("function"), str):
        problems.append("aggregation function must be text")
    if not isinstance(raw.get("column"), str):
        problems.append("aggregation column must be text")
    return problems


def _filter_problems(raw: Any, index: int) -> list[str]:
    label = f"filters[{index}]"
    if not isinstance(raw, dict):
        return [f"{label} must be an object with column, operator and value"]
    problems = [f"{label}: {problem}" for problem in _key_problems(raw, FILTER_KEYS, "filter")]
    if not isinstance(raw.get("column"), str):
        problems.append(f"{label}: column must be text")
    if not isinstance(raw.get("operator"), str):
        problems.append(f"{label}: operator must be text")
    return problems

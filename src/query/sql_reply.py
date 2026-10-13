"""Stage 12: read the SQL generator's reply as {"sql", "params"}. Only the shape is checked here."""

from __future__ import annotations

import json
from typing import Any

from src.query.plan import FENCE, PlanError, _DuplicateKeyError, _key_problems, _no_duplicate_keys
from src.query.shapes import SQL_REPLY_KEYS


def parse_sql_reply(raw: str) -> dict[str, Any]:
    """Return {"sql": str, "params": list}. Raise PlanError for anything else."""
    body = raw.strip()
    match = FENCE.match(body)
    if match:
        body = match.group(1)
    try:
        parsed = json.loads(body, object_pairs_hook=_no_duplicate_keys)
    except _DuplicateKeyError as error:
        raise PlanError([f"duplicate key in reply: {error}"]) from None
    except json.JSONDecodeError as error:
        raise PlanError([f"reply is not one JSON object: {error.msg}"]) from None

    if not isinstance(parsed, dict):
        raise PlanError(["reply must be one JSON object"])

    problems = _key_problems(parsed, SQL_REPLY_KEYS, "reply")
    if not isinstance(parsed.get("sql"), str) or not parsed.get("sql", "").strip():
        problems.append("sql must be non-empty text")
    params = parsed.get("params")
    if not isinstance(params, list):
        problems.append("params must be a list")
    if problems:
        raise PlanError(problems)
    return {"sql": parsed["sql"], "params": params}

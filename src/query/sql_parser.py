"""Strict parser for the SQL Generator output (D3).

Accepts exactly one JSON object with keys sql, params, and assumptions. Placeholder
count is checked against params before any AST validation runs.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

MAX_SQL_OUTPUT_CHARS = 20000
EXPECTED_KEYS = frozenset({"sql", "params", "assumptions"})
_FENCE = re.compile(r"^```(?:json)?[ \t]*\r?\n(?P<body>.*)\r?\n```$", re.DOTALL)
_DOLLAR_TAG = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


class SqlParseError(ValueError):
    category = "invalid_sql"

    def __init__(self, path: str, reason: str):
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


@dataclass(frozen=True)
class GeneratedSql:
    sql: str
    params: list
    assumptions: list


def parse_generated_sql(text: str) -> GeneratedSql:
    """Parse and shape-check generator text. Raises SqlParseError on any malformed output."""
    if not isinstance(text, str):
        raise SqlParseError("sql_output", "generator output must be text")
    if len(text) > MAX_SQL_OUTPUT_CHARS:
        raise SqlParseError("sql_output", f"generator output exceeds {MAX_SQL_OUTPUT_CHARS} characters")
    obj = _decode(_unwrap(text))
    if not isinstance(obj, dict):
        raise SqlParseError("sql_output", "generator output must be a JSON object")
    if set(obj) != EXPECTED_KEYS:
        raise SqlParseError("sql_output", "output must have exactly the keys sql, params, assumptions")

    sql = obj["sql"]
    if not isinstance(sql, str) or not sql.strip():
        raise SqlParseError("sql", "sql must be a non-empty string")
    params = _check_params(obj["params"])
    assumptions = _check_assumptions(obj["assumptions"])

    placeholders = count_placeholders(sql)
    if placeholders != len(params):
        raise SqlParseError(
            "params",
            f"sql has {placeholders} placeholders but {len(params)} params were given",
        )
    return GeneratedSql(sql=sql.strip(), params=params, assumptions=assumptions)


def count_placeholders(sql: str) -> int:
    """Count bare positional `?` tokens, skipping literals, identifiers, and comments."""
    count = 0
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            escapes = i > 0 and sql[i - 1] in "eE" and (i == 1 or not _is_ident(sql[i - 2]))
            i = _skip_quoted(sql, i, "'", escapes)
        elif ch == '"':
            i = _skip_quoted(sql, i, '"', False)
        elif sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end == -1 else end + 1
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end == -1:
                raise SqlParseError("sql", "unterminated block comment")
            i = end + 2
        elif ch == "$" and _starts_dollar_quote(sql, i):
            tag = _DOLLAR_TAG.match(sql, i).group(0)
            close = sql.find(tag, i + len(tag))
            if close == -1:
                raise SqlParseError("sql", "unterminated dollar-quoted string")
            i = close + len(tag)
        else:
            if ch == "?":
                count += 1
            i += 1
    return count


def _unwrap(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        raise SqlParseError("sql_output", "generator output is empty")
    if "```" in stripped:
        match = _FENCE.match(stripped)
        if match is None:
            raise SqlParseError("sql_output", "generator output has text outside a single fenced block")
        return match.group("body").strip()
    return stripped


def _decode(body: str) -> Any:
    decoder = json.JSONDecoder(object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant)
    try:
        obj, end = decoder.raw_decode(body)
    except json.JSONDecodeError as exc:
        raise SqlParseError("sql_output", f"invalid JSON at character {exc.pos}") from exc
    if body[end:].strip():
        raise SqlParseError("sql_output", "unexpected content after the JSON object")
    return obj


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SqlParseError("sql_output", f"duplicate key '{key}'")
        result[key] = value
    return result


def _reject_constant(name: str) -> Any:
    raise SqlParseError("sql_output", f"non-finite number '{name}' is not allowed")


def _check_params(value: Any) -> list:
    if not isinstance(value, list):
        raise SqlParseError("params", "params must be a list")
    for index, item in enumerate(value):
        if item is None or isinstance(item, (str, bool)):
            continue
        if isinstance(item, int):
            continue
        if isinstance(item, float) and math.isfinite(item):
            continue
        raise SqlParseError(f"params[{index}]", "params must be strings, numbers, booleans, or null")
    return list(value)


def _check_assumptions(value: Any) -> list:
    if not isinstance(value, list):
        raise SqlParseError("assumptions", "assumptions must be a list")
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise SqlParseError(f"assumptions[{index}]", "each assumption must be a string")
    return list(value)


def _skip_quoted(text: str, start: int, quote: str, backslash_escapes: bool) -> int:
    """Return the index after the closing quote, honouring doubled-quote escapes."""
    n = len(text)
    j = start + 1
    while j < n:
        ch = text[j]
        if backslash_escapes and ch == "\\":
            j += 2
            continue
        if ch == quote:
            if j + 1 < n and text[j + 1] == quote:
                j += 2
                continue
            return j + 1
        j += 1
    raise SqlParseError("sql", "unterminated quoted literal or identifier")


def _starts_dollar_quote(sql: str, i: int) -> bool:
    if i > 0 and _is_ident(sql[i - 1]):
        return False
    return _DOLLAR_TAG.match(sql, i) is not None


def _is_ident(ch: str) -> bool:
    return ch.isalnum() or ch == "_"

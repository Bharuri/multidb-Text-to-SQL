from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb


class ExecuteError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def execute(built: dict[str, Any], db_file: str | Path, row_limit: int, aggregate: bool) -> dict[str, Any]:
    """Run validated SQL on a read-only connection. Row queries return at most row_limit rows.

    Callers must only pass SQL that the validator accepted. A single-value aggregate fetches one row.
    """
    try:
        connection = duckdb.connect(str(db_file), read_only=True)
    except duckdb.Error as error:
        raise ExecuteError("The database could not be opened.") from error
    try:
        cursor = connection.execute(built["sql"], built["params"])
        columns = [description[0] for description in cursor.description]
        fetch = 1 if aggregate else row_limit + 1
        rows = [list(row) for row in cursor.fetchmany(fetch)]
    except duckdb.Error as error:
        raise ExecuteError(f"The query could not be run: {error}") from error
    finally:
        connection.close()

    truncated = not aggregate and len(rows) > row_limit
    rows = rows[:row_limit] if not aggregate else rows
    result: dict[str, Any] = {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
    }
    if aggregate:
        result["value"] = rows[0][0] if rows and rows[0] else None
    return result


def check_manual_sql(sql: str) -> str:
    """Accept one read-only SELECT or WITH statement typed by the user. Returns the trimmed SQL."""
    text = sql.strip().rstrip(";").strip()
    if not text:
        raise ExecuteError("Enter a SQL query first.")
    if ";" in text:
        raise ExecuteError("Run one SQL statement at a time. A semicolon inside a string literal also counts.")
    if text.split(None, 1)[0].upper() not in ("SELECT", "WITH"):
        raise ExecuteError("Only read-only SELECT queries can run here.")
    return text


def run_manual_sql(sql: str, db_file: str | Path, row_limit: int) -> dict[str, Any]:
    """Run a user-typed read-only query. The connection is read-only, so writes fail even if they pass the check."""
    built = {"sql": check_manual_sql(sql), "params": []}
    return execute(built, db_file, row_limit, aggregate=False)

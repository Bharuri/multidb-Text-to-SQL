from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb


class ExecuteError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def execute(built: dict[str, Any], db_file: str | Path, row_limit: int, aggregate: bool) -> dict[str, Any]:
    """Run validated SQL on a read-only connection. Row queries return at most row_limit rows."""
    connection = None
    try:
        connection = duckdb.connect(str(db_file), read_only=True)
        cursor = connection.execute(built["sql"], built["params"])
        columns = [description[0] for description in cursor.description]
        fetch = 1 if aggregate else row_limit + 1
        rows = [list(row) for row in cursor.fetchmany(fetch)]
    except duckdb.Error as error:
        raise ExecuteError(str(error)) from error
    finally:
        if connection is not None:
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

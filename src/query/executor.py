"""Read-only execution of validated SQL (E1).

The connection is opened read-only, parameters are passed separately from the
SQL text, the result is capped, and the query is interrupted after a timeout.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb


class ExecutionError(Exception):
    """Runtime failure. `reason` is safe to show; `diagnostic` is for logs only."""

    category = "execution_error"

    def __init__(self, reason: str, diagnostic: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.diagnostic = diagnostic


@dataclass(frozen=True)
class ExecutionResult:
    columns: list[str]
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False


def connect_read_only(database_file: Path | str, schema: str | None = None) -> duckdb.DuckDBPyConnection:
    """Open a read-only connection whose default schema is the dataset's schema.

    The validator accepts unqualified table names, so they must resolve on the
    same connection that runs them.
    """
    connection = duckdb.connect(str(database_file), read_only=True)
    if schema:
        try:
            connection.execute('SET schema = "' + schema.replace('"', '""') + '"')
        except duckdb.Error:
            connection.close()
            raise
    return connection


def execute_read_only(
    database_file: Path,
    sql: str,
    params: Sequence[Any],
    max_rows: int,
    timeout_seconds: float,
    schema: str | None = None,
) -> ExecutionResult:
    if max_rows < 1:
        raise ValueError("max_rows must be positive")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    try:
        connection = connect_read_only(database_file, schema)
    except duckdb.Error as exc:
        raise ExecutionError("the database could not be opened", type(exc).__name__) from None

    timer = threading.Timer(timeout_seconds, connection.interrupt)
    try:
        timer.start()
        cursor = connection.execute(sql, list(params))
        columns = [description[0] for description in cursor.description or []]
        fetched = cursor.fetchmany(max_rows + 1)
    except duckdb.InterruptException:
        raise ExecutionError(f"the query exceeded the {timeout_seconds:g} second timeout") from None
    except duckdb.Error as exc:
        raise ExecutionError("the query failed at runtime", f"{type(exc).__name__}: {exc}") from None
    finally:
        timer.cancel()
        connection.close()

    truncated = len(fetched) > max_rows
    rows = [list(row) for row in fetched[:max_rows]]
    return ExecutionResult(columns=columns, rows=rows, row_count=len(rows), truncated=truncated)

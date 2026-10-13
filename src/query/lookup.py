"""Bounded, parameterized value lookup for columns above the stored-value threshold."""

from __future__ import annotations

from pathlib import Path

import duckdb

from src.query.catalog import PROJECT_ROOT, Resolved
from src.query.matcher import ValueLookup
from src.query.sql_builder import quote_identifier

# Two rows are enough to tell "none", "one" and "several" apart.
LOOKUP_LIMIT = 2


def lookup_values(db_path: Path, schema: str, table: str, column: str, value: str) -> list[str]:
    """Return up to LOOKUP_LIMIT distinct stored values equal to value, ignoring case."""
    column_sql = quote_identifier(column)
    # DuckDB names the catalog after the file, so the file stem is the catalog name.
    source = f"{quote_identifier(db_path.stem)}.{quote_identifier(schema)}.{quote_identifier(table)}"
    sql = (
        f"SELECT DISTINCT {column_sql} FROM {source} "
        f"WHERE LOWER({column_sql}) = LOWER(?) LIMIT {LOOKUP_LIMIT}"
    )
    connection = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = connection.execute(sql, [value]).fetchall()
    finally:
        connection.close()
    return [row[0] for row in rows]


def make_lookup(resolved: Resolved, root: Path = PROJECT_ROOT) -> ValueLookup:
    db_path = root / resolved.db_file
    schema = resolved.physical["schema"]

    def lookup(table: str, column: str, value: str) -> list[str]:
        return lookup_values(db_path, schema, table, column, value)

    return lookup

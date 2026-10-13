"""Creates DuckDB databases, schemas, and tables."""

import re
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pandas as pd
from pandas.api.types import infer_dtype


def normalize_identifier(name: str) -> str:
    cleaned = re.sub(r"[\W_]+", "_", name.strip()).strip("_")
    if not cleaned:
        return "unnamed"
    if cleaned[0].isdigit():
        return f"_{cleaned}"
    return cleaned


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def workbook_name(workbook_path: Path) -> str:
    return normalize_identifier(workbook_path.stem)


def open_workbook_database(
    database_dir: Path, workbook_path: Path
) -> duckdb.DuckDBPyConnection:
    database_dir.mkdir(parents=True, exist_ok=True)
    name = workbook_name(workbook_path)
    connection = duckdb.connect(str(database_dir / f"{name}.duckdb"))
    try:
        connection.execute(f"CREATE SCHEMA IF NOT EXISTS {quote_identifier(name)}")
    except Exception:
        connection.close()
        raise
    return connection


def unique_names(names: list[str]) -> list[str]:
    """Normalize names into identifiers, suffixing duplicates like _2, _3."""
    used: set[str] = set()
    unique: list[str] = []
    for name in names:
        base = normalize_identifier(name)
        candidate = base
        suffix = 2
        while candidate.casefold() in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        used.add(candidate.casefold())
        unique.append(candidate)
    return unique


def _as_text(value: object) -> str | None:
    if pd.isna(value):
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _text_for_mixed_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Store mixed-type object columns as text, since DuckDB cannot infer one type for them."""
    result = frame.copy()
    for column in result.columns:
        series = result[column]
        if series.dtype == object and infer_dtype(series, skipna=True).startswith("mixed"):
            result[column] = series.map(_as_text)
    return result


def write_table(
    connection: duckdb.DuckDBPyConnection,
    schema: str,
    table: str,
    frame: pd.DataFrame,
) -> None:
    columns = unique_names([str(column) for column in frame.columns])
    data = _text_for_mixed_columns(frame.set_axis(columns, axis=1))
    # The catalog is the database file name, which matches the schema name.
    # Qualifying with the catalog avoids DuckDB's catalog/schema ambiguity.
    target = ".".join(quote_identifier(part) for part in (schema, schema, table))
    connection.register("incoming_sheet", data)
    try:
        connection.execute(
            f"CREATE OR REPLACE TABLE {target} AS SELECT * FROM incoming_sheet"
        )
    finally:
        connection.unregister("incoming_sheet")


@dataclass
class LoadResult:
    database: Path
    tables: list[str] = field(default_factory=list)
    sheet_errors: dict[str, str] = field(default_factory=dict)


def load_workbook(
    database_dir: Path,
    workbook_path: Path,
    sheets: dict[str, pd.DataFrame],
) -> LoadResult:
    schema = workbook_name(workbook_path)
    result = LoadResult(database=database_dir / f"{schema}.duckdb")
    connection = open_workbook_database(database_dir, workbook_path)
    try:
        table_names = unique_names(list(sheets))
        for sheet_name, table in zip(sheets, table_names):
            try:
                write_table(connection, schema, table, sheets[sheet_name])
                result.tables.append(table)
            except Exception as exc:
                result.sheet_errors[sheet_name] = str(exc)
    finally:
        connection.close()
    return result

"""Full physical schema read from `specs/properties/physical_requirements/<key>.md`.

The planner needs every table and column of the selected database, not only the lexically
retrieved ones. This module reads that list from the `## Columns` section and checks it against
the physical YAML, so the two sources cannot drift silently.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from src.query.config import MetadataMismatchError

SchemaMap = dict[str, dict[str, str]]  # table -> column -> type

_TABLE_HEADING = re.compile(r"^###\s+`([^`]+)`\s*$")
_COLUMN_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*([^|]+?)\s*\|")


def parse_physical_markdown(text: str, file: str) -> SchemaMap:
    """Return table -> column -> type from the `## Columns` section.

    Only `### \\`Table\\`` headings and rows whose first cell is a backticked column name are read.
    Any other line, including the header row, is ignored.
    """
    tables: SchemaMap = {}
    in_columns = False
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            in_columns = line.strip() == "## Columns"
            current = None
            continue
        if not in_columns:
            continue
        heading = _TABLE_HEADING.match(line)
        if heading:
            current = heading.group(1)
            if current in tables:
                raise MetadataMismatchError(f"{file}: table {current!r} is listed more than once.")
            tables[current] = {}
            continue
        row = _COLUMN_ROW.match(line)
        if row and current is not None:
            name, column_type = row.group(1), row.group(2)
            if name in tables[current]:
                raise MetadataMismatchError(f"{file}: column {current}.{name} is listed more than once.")
            tables[current][name] = column_type
    if not tables:
        raise MetadataMismatchError(f"{file}: no tables found under '## Columns'.")
    return tables


def check_against_physical(markdown: SchemaMap, physical: Mapping[str, Mapping[str, str]], file: str) -> None:
    """Raise `MetadataMismatchError` when the markdown and the physical YAML disagree."""
    errors: list[str] = []
    for table in sorted(set(markdown) - set(physical)):
        errors.append(f"table {table!r} is in {file} but not in the physical mapping.")
    for table in sorted(set(physical) - set(markdown)):
        errors.append(f"table {table!r} is in the physical mapping but not in {file}.")
    for table in sorted(set(markdown) & set(physical)):
        md_columns, yaml_columns = markdown[table], physical[table]
        for column in sorted(set(md_columns) - set(yaml_columns)):
            errors.append(f"column {table}.{column} is in {file} but not in the physical mapping.")
        for column in sorted(set(yaml_columns) - set(md_columns)):
            errors.append(f"column {table}.{column} is in the physical mapping but not in {file}.")
        for column in sorted(set(md_columns) & set(yaml_columns)):
            if md_columns[column].upper() != str(yaml_columns[column]).upper():
                errors.append(
                    f"column {table}.{column} has type {md_columns[column]!r} in {file} "
                    f"but {yaml_columns[column]!r} in the physical mapping."
                )
    if errors:
        raise MetadataMismatchError(f"{file} does not match the physical mapping: " + " ".join(errors))

"""Deterministic checks on generated SQL: column names and literal values.

The SQL generator writes free SQL, so these checks run after the AST check. They use DuckDB's
parse tree (see sql_ast.serialize_sql) and the catalog, and reject only what they can verify:

- a column reference that is neither a physical column of the dataset nor an alias or CTE output;
- a string parameter compared with a column whose semantic metadata lists its values, when the
  value is not in that list (matched without regard to case).

Anything else is left to DuckDB and the executor.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

import duckdb

from src.query.catalog import Catalog
from src.query.sql_ast import SqlAstError, serialize_sql

# A double-quoted identifier (with "" escapes) or a bare word. Used to read each token DuckDB reports.
_IDENTIFIER = re.compile(r'"(?:[^"]|"")*"|[A-Za-z_][A-Za-z0-9_$]*')
_COMPARE_TYPES = frozenset({"COMPARE_EQUAL", "COMPARE_NOTEQUAL"})
_IN_TYPES = frozenset({"COMPARE_IN", "COMPARE_NOT_IN"})
_VALUE_TYPES = frozenset({"VALUE_PARAMETER", "VALUE_CONSTANT"})


def _walk(node: Any):
    """Yield every dict in the parse tree."""
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            yield current
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def _lower(names: Sequence[str]) -> set[str]:
    return {name.lower() for name in names}


class _Names:
    """Table aliases, CTE names, and output aliases found in one parse tree."""

    def __init__(self, tree: dict[str, Any]) -> None:
        self.base_tables: dict[str, str] = {}  # alias or table name (lower) -> table name
        # An alias reused for different tables in different scopes cannot be resolved without scoping.
        self.ambiguous_aliases: set[str] = set()
        self.cte_names: set[str] = set()
        self.output_aliases: set[str] = set()
        for node in _walk(tree):
            if node.get("type") == "BASE_TABLE" and isinstance(node.get("table_name"), str):
                table = node["table_name"]
                self._bind(table.lower(), table)
                alias = node.get("alias")
                if isinstance(alias, str) and alias:
                    self._bind(alias.lower(), table)
                self.output_aliases |= _lower(node.get("column_name_alias") or [])
            alias = node.get("alias")
            if isinstance(alias, str) and alias and node.get("type") != "BASE_TABLE":
                self.output_aliases.add(alias.lower())
            cte_map = node.get("cte_map")
            if isinstance(cte_map, dict):
                for entry in cte_map.get("map", []):
                    if isinstance(entry, dict) and isinstance(entry.get("key"), str):
                        self.cte_names.add(entry["key"].lower())

    def _bind(self, name: str, table: str) -> None:
        previous = self.base_tables.get(name)
        if previous is not None and previous != table:
            self.ambiguous_aliases.add(name)
        self.base_tables[name] = table


def _physical_columns(catalog: Catalog) -> dict[str, set[str]]:
    """Table name (lower) -> its physical column names (lower)."""
    return {
        table.lower(): {name.lower() for name in info.columns} for table, info in catalog.tables.items()
    }


def _query_outputs(node: Any, aliases: Sequence[Any]) -> set[str] | None:
    """Output column names of a CTE body, or None when they cannot be known (for example `SELECT *`)."""
    if aliases:
        return _lower([alias for alias in aliases if isinstance(alias, str)])
    while isinstance(node, dict) and node.get("type") == "SET_OPERATION_NODE":
        node = node.get("left")
    if not isinstance(node, dict) or node.get("type") != "SELECT_NODE":
        return None
    outputs: set[str] = set()
    for item in node.get("select_list", []):
        if not isinstance(item, dict):
            return None
        alias = item.get("alias")
        if isinstance(alias, str) and alias:
            outputs.add(alias.lower())
        elif item.get("type") == "STAR":
            return None
        elif item.get("type") == "COLUMN_REF":
            parts = [part for part in item.get("column_names", []) if isinstance(part, str)]
            if not parts or parts[-1] == "*":
                return None
            outputs.add(parts[-1].lower())
    return outputs


def _cte_outputs(tree: dict[str, Any]) -> dict[str, set[str] | None]:
    """CTE name (lower) -> its output column names (lower), or None when unknown."""
    outputs: dict[str, set[str] | None] = {}
    for node in _walk(tree):
        cte_map = node.get("cte_map")
        if not isinstance(cte_map, dict):
            continue
        for entry in cte_map.get("map", []):
            if not (isinstance(entry, dict) and isinstance(entry.get("key"), str)):
                continue
            value = entry.get("value") if isinstance(entry.get("value"), dict) else {}
            query = value.get("query") if isinstance(value.get("query"), dict) else {}
            found = _query_outputs(query.get("node"), value.get("aliases") or [])
            name = entry["key"].lower()
            # The same CTE name in two scopes cannot be resolved here, so its columns are not checked.
            outputs[name] = found if name not in outputs or outputs[name] == found else None
    return outputs


def _column_hint(column: str, catalog: Catalog) -> str:
    """Suggest physical column names that match the given one ignoring case, spaces, and underscores."""
    key = _squash(column)
    matches = sorted(
        {name for info in catalog.tables.values() for name in info.columns if _squash(name) == key}
    )
    if not matches:
        return ""
    return " (did you mean " + " or ".join(repr(name) for name in matches) + "?)"


def _squash(name: str) -> str:
    return re.sub(r"[\s_]+", "", name).lower()


def rewrite_column_names(sql: str, catalog: Catalog) -> tuple[str, list[tuple[str, str]]]:
    """Rename identifiers that match one physical column only by case, spaces, or underscores.

    Returns the rewritten SQL and the (written, physical) pairs. An identifier is changed only when
    exactly one physical column matches it, and never when it names a table, CTE, or output alias.
    Everything else is left for check_column_names to reject.
    """
    try:
        tree = serialize_sql(sql)
    except SqlAstError:
        return sql, []
    names = _Names(tree)
    physical = _physical_columns(catalog)
    known = set().union(*physical.values()) if physical else set()
    table_names = set(physical) | set(names.base_tables) | names.cte_names | names.output_aliases
    by_squash: dict[str, set[str]] = {}
    for info in catalog.tables.values():
        for column in info.columns:
            by_squash.setdefault(_squash(column), set()).add(column)
    edits: list[tuple[int, int, str, str]] = []
    for position, token_type in duckdb.tokenize(sql):
        if token_type != duckdb.token_type.identifier:
            continue
        match = _IDENTIFIER.match(sql, position)
        if match is None:
            continue
        written = match.group(0)
        name = written[1:-1].replace('""', '"') if written.startswith('"') else written
        lower = name.lower()
        if lower in known or lower in table_names:
            continue
        candidates = by_squash.get(_squash(name), set())
        if len(candidates) == 1:
            edits.append((match.start(), match.end(), name, next(iter(candidates))))
    if not edits:
        return sql, []
    pieces: list[str] = []
    last = 0
    for start, end, _, physical_name in edits:
        pieces.append(sql[last:start])
        pieces.append('"' + physical_name.replace('"', '""') + '"')
        last = end
    pieces.append(sql[last:])
    return "".join(pieces), [(written, target) for _, _, written, target in edits]


def check_column_names(tree: dict[str, Any], catalog: Catalog) -> None:
    """Reject column references the dataset and the query do not define."""
    names = _Names(tree)
    physical = _physical_columns(catalog)
    all_columns = set().union(*physical.values()) if physical else set()
    known = all_columns | names.output_aliases | names.cte_names
    cte_outputs = _cte_outputs(tree)
    # Every unknown column is reported together so one repair can fix them all.
    problems: dict[str, str] = {}
    for node in _walk(tree):
        if node.get("type") != "COLUMN_REF":
            continue
        parts = [part for part in node.get("column_names", []) if isinstance(part, str)]
        if not parts or parts == ["*"] or parts[-1] == "*":
            continue
        *qualifiers, column = parts
        column_lower = column.lower()
        path = f"column {'.'.join(parts)}"
        if qualifiers:
            qualifier = qualifiers[-1].lower()
            if qualifier not in names.base_tables and qualifier not in names.cte_names and qualifier not in known:
                raise SqlAstError("invalid_sql", path, f"unknown table or alias '{qualifiers[-1]}'")
            if qualifier in names.ambiguous_aliases:
                continue
            table = names.base_tables.get(qualifier)
            cte_columns = cte_outputs.get(table.lower()) if table is not None else None
            if cte_columns is not None:
                if column_lower in cte_columns:
                    continue
                problems[path] = f"unknown column '{column}' in CTE '{table}'"
                continue
            # A CTE reference is also a BASE_TABLE in the parse tree, but it has no physical columns.
            table_columns = physical.get(table.lower()) if table is not None else None
            if table_columns is not None and column_lower not in table_columns:
                if column_lower not in names.output_aliases:
                    problems[path] = (
                        f"unknown column '{column}' in table '{table}'{_column_hint(column, catalog)}"
                    )
                continue
        if column_lower not in known:
            problems[path] = f"unknown column '{column}'{_column_hint(column, catalog)}"
    if problems:
        raise SqlAstError("invalid_sql", ", ".join(problems), "; ".join(problems.values()))


def _column_target(
    parts: Sequence[str], names: _Names, catalog: Catalog
) -> tuple[str, str] | None:
    """Return (table, column) for a column reference that resolves to one catalog column."""
    *qualifiers, column = parts
    column_lower = column.lower()
    if qualifiers:
        qualifier = qualifiers[-1].lower()
        if qualifier in names.ambiguous_aliases:
            return None
        table = names.base_tables.get(qualifier)
        if table is None:
            return None
        info = catalog.tables.get(table)
        if info is None:
            return None
        matches = [name for name in info.columns if name.lower() == column_lower]
        return (table, matches[0]) if matches else None
    candidates = []
    for table in dict.fromkeys(names.base_tables.values()):
        info = catalog.tables.get(table)
        if info is None:
            continue
        matches = [name for name in info.columns if name.lower() == column_lower]
        if matches:
            candidates.append((table, matches[0]))
    unique = set(candidates)
    return next(iter(unique)) if len(unique) == 1 else None


def _comparison_pairs(tree: dict[str, Any]):
    """Yield (column parts, operand node) for each equality or IN comparison with a parameter or a constant."""
    for node in _walk(tree):
        # Constants carry a LogicalType dict in "type", so only string kinds are comparable.
        node_type = node.get("type")
        if not isinstance(node_type, str):
            continue
        if node.get("class") == "COMPARISON" and node_type in _COMPARE_TYPES:
            left, right = node.get("left"), node.get("right")
            for column, operand in ((left, right), (right, left)):
                if (
                    isinstance(column, dict)
                    and column.get("type") == "COLUMN_REF"
                    and isinstance(operand, dict)
                    and operand.get("type") in _VALUE_TYPES
                ):
                    yield column.get("column_names", []), operand
        elif node_type in _IN_TYPES:
            children = node.get("children", [])
            if children and isinstance(children[0], dict) and children[0].get("type") == "COLUMN_REF":
                for operand in children[1:]:
                    if isinstance(operand, dict) and operand.get("type") in _VALUE_TYPES:
                        yield children[0].get("column_names", []), operand


def _operand_value(operand: dict[str, Any], params: Sequence[Any]) -> tuple[str, Any] | None:
    """Return (label, value) for a parameter or inline constant, or None when it is not checkable."""
    if operand.get("type") == "VALUE_PARAMETER":
        identifier = str(operand.get("identifier", ""))
        if identifier.isdigit() and 1 <= int(identifier) <= len(params):
            return f"parameter {identifier}", params[int(identifier) - 1]
        return None
    raw = (operand.get("value") or {}).get("value")
    return "inline value", raw


def check_literal_values(tree: dict[str, Any], params: Sequence[Any], catalog: Catalog) -> None:
    """Reject a string value that is not one of the listed values of the column it is compared with.

    Applies to both `?` parameters and inline string literals, so the check does not depend on how the value is written.
    """
    names = _Names(tree)
    for parts, operand in _comparison_pairs(tree):
        found = _operand_value(operand, params)
        if found is None:
            continue
        label, value = found
        if not isinstance(value, str):
            continue
        target = _column_target([p for p in parts if isinstance(p, str)], names, catalog)
        if target is None:
            continue
        semantic = catalog.semantic_for(*target)
        if semantic is None or not semantic.values:
            continue
        allowed = {item.strip().lower() for item in semantic.values}
        if value.strip().lower() not in allowed:
            table, column = target
            raise SqlAstError(
                "invalid_sql",
                label,
                f"value {value!r} is not a listed value of {table}.{column}",
            )


def check_sql_names_and_values(sql: str, params: Sequence[Any], catalog: Catalog) -> None:
    """Run both checks. Raises SqlAstError with a path and a reason the repair prompt can quote."""
    tree = serialize_sql(sql)
    check_column_names(tree, catalog)
    check_literal_values(tree, params, catalog)

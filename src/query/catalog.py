"""Registry lookup and metadata loading for stage 2 (resolve). YAML files are cached by modified time."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_KEYS = ("physical_mapping", "semantic_mapping", "database_file")

_cache: dict[Path, tuple[int, Any]] = {}


class CatalogError(Exception):
    """A database cannot be resolved. The message is shown to the user."""


@dataclass(frozen=True)
class Resolved:
    database: str
    registry_entry: dict[str, str]
    physical_file: str
    semantic_file: str
    db_file: str
    physical: dict[str, Any]
    semantic: dict[str, Any]

    @property
    def table_count(self) -> int:
        return len(self.physical["tables"])

    @property
    def column_count(self) -> int:
        return sum(len(table["columns"]) for table in self.physical["tables"])


def load_yaml(path: Path) -> Any:
    """Parse a YAML file again only when its modified time has changed."""
    stamp = path.stat().st_mtime_ns
    cached = _cache.get(path)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    try:
        with path.open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise CatalogError(f"Cannot read {path.name}: {type(error).__name__}") from None
    _cache[path] = (stamp, data)
    return data


def resolve(database: str, root: Path = PROJECT_ROOT) -> Resolved:
    config_path = root / "config.yaml"
    if not config_path.is_file():
        raise CatalogError("config.yaml not found")
    registry = (load_yaml(config_path) or {}).get("databases") or {}

    if database not in registry:
        raise CatalogError(f"Unknown database: {database}. Registry has: {', '.join(registry)}")
    entry = registry[database]
    missing = [key for key in REGISTRY_KEYS if key not in entry]
    if missing:
        raise CatalogError(f"Registry entry for {database} is missing: {', '.join(missing)}")

    for key in REGISTRY_KEYS:
        if not (root / entry[key]).is_file():
            raise CatalogError(f"File not found for {database}: {entry[key]}")

    physical = load_yaml(root / entry["physical_mapping"])
    semantic = load_yaml(root / entry["semantic_mapping"])
    _check_metadata_shape(database, physical, semantic)

    expected = Path(entry["database_file"]).as_posix()
    for label, value in (("physical file", physical.get("file")), ("semantic database_file", semantic.get("database_file"))):
        if value is None or Path(str(value)).as_posix() != expected:
            raise CatalogError(f"Path mismatch for {database}: {label} is {value}, registry has {entry['database_file']}")

    return Resolved(
        database=database,
        registry_entry=dict(entry),
        physical_file=entry["physical_mapping"],
        semantic_file=entry["semantic_mapping"],
        db_file=entry["database_file"],
        physical=physical,
        semantic=semantic,
    )


def _check_metadata_shape(database: str, physical: Any, semantic: Any) -> None:
    """Reject metadata that is not a mapping, has the wrong database or schema key, or has malformed tables and columns."""
    for label, data in (("physical mapping", physical), ("semantic mapping", semantic)):
        if not isinstance(data, dict):
            raise CatalogError(f"Malformed {label} for {database}: top level must be a mapping")

    for label, data in (("physical mapping", physical), ("semantic mapping", semantic)):
        if data.get("database") != database:
            raise CatalogError(f"Schema key mismatch for {database}: {label} database is {data.get('database')}")
    schema = physical.get("schema")
    if not isinstance(schema, str) or not schema:
        raise CatalogError(f"Malformed physical mapping for {database}: schema must be text")
    if semantic.get("schema") != schema:
        raise CatalogError(f"Schema key mismatch for {database}: semantic schema is {semantic.get('schema')}, physical is {schema}")

    tables = physical.get("tables")
    if not isinstance(tables, list) or not tables:
        raise CatalogError(f"Malformed physical mapping for {database}: tables must be a non-empty list")
    table_names: set[str] = set()
    for table in tables:
        if not isinstance(table, dict) or not isinstance(table.get("name"), str) or not table["name"]:
            raise CatalogError(f"Malformed physical mapping for {database}: every table needs a text name")
        if table["name"] in table_names:
            raise CatalogError(f"Malformed physical mapping for {database}: duplicate table {table['name']}")
        table_names.add(table["name"])
        columns = table.get("columns")
        if not isinstance(columns, list) or not columns:
            raise CatalogError(f"Malformed physical mapping for {database}: {table['name']} needs a non-empty columns list")
        column_names: set[str] = set()
        for column in columns:
            if not isinstance(column, dict) or not isinstance(column.get("name"), str) or not column["name"]:
                raise CatalogError(f"Malformed physical mapping for {database}: every column in {table['name']} needs a text name")
            if column["name"] in column_names:
                raise CatalogError(f"Malformed physical mapping for {database}: duplicate column {column['name']} in {table['name']}")
            column_names.add(column["name"])
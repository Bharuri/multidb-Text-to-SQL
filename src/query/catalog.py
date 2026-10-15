"""Normalized metadata catalog and per-database cache (B2).

`load_catalog` validates and parses one database's physical and semantic metadata into
immutable structures. `CatalogCache` keeps one catalog per registry key and only reloads
a database when its own metadata files change (stat-based fingerprint, so unchanged
requests never re-read YAML).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from src.mapping.metadata_validator import validate_metrics
from src.query.config import (
    DatabaseEntry,
    MetadataMismatchError,
    MissingFileError,
    Registry,
    select_database,
)
from src.query.physical_md import (
    SchemaMap,
    check_against_physical,
    parse_physical_markdown,
)


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    type: str
    nullable: bool
    has_nulls: bool
    is_unique: bool


@dataclass(frozen=True)
class TableInfo:
    name: str
    source_sheet: str
    row_count: int
    columns: dict[str, ColumnInfo]


@dataclass(frozen=True)
class SemanticColumn:
    table: str
    column: str
    data_type: str
    column_class: str
    synonyms: tuple[str, ...]
    values: tuple[str, ...]
    flags: tuple[str, ...]
    raw: dict[str, Any] = field(repr=False, compare=False, default_factory=dict)


@dataclass(frozen=True)
class Endpoint:
    table: str
    column: str
    # Approved join-key operations, applied in order: "lower", "trim", "cast BIGINT", ...
    normalize: tuple[str, ...] = ()


@dataclass(frozen=True)
class Relationship:
    name: str
    status: str
    meaning: str
    cardinality: str
    enabled: bool
    join_policy: str
    fan_out: str
    child: Endpoint
    parent: Endpoint


@dataclass(frozen=True)
class Metric:
    """A validated metric definition. `raw` holds the kind-specific spec (numerator, value, ...)."""

    name: str
    status: str
    kind: str
    source: str  # "semantic" (generated from requirements) or "business" (accepted proposal)
    synonyms: tuple[str, ...]
    raw: dict[str, Any] = field(repr=False, compare=False, default_factory=dict)


def _normalize_term(term: str) -> str:
    return term.strip().lower().replace(" ", "_")


def _endpoint(raw: dict[str, Any], ops: list[str]) -> Endpoint:
    return Endpoint(raw["table"], raw["column"], tuple(ops))


@dataclass(frozen=True)
class Catalog:
    key: str
    schema: str
    database_file: str
    tables: dict[str, TableInfo]
    semantic_columns: tuple[SemanticColumn, ...]
    relationships: tuple[Relationship, ...]
    numeric_operators: tuple[dict[str, Any], ...]
    fingerprint: tuple[tuple[str, int, int], ...]
    metrics: tuple[Metric, ...] = ()
    # Every table and column with its type (table -> column -> type), for the planner.
    full_schema: SchemaMap = field(default_factory=dict)

    @property
    def enabled_relationships(self) -> tuple[Relationship, ...]:
        return tuple(r for r in self.relationships if r.enabled)

    def semantic_for(self, table: str, column: str) -> SemanticColumn | None:
        for item in self.semantic_columns:
            if item.table == table and item.column == column:
                return item
        return None

    def metric_for(self, term: str) -> Metric | None:
        """Match a metric by name or synonym, ignoring case, spaces, and underscores."""
        wanted = _normalize_term(term)
        for metric in self.metrics:
            if wanted == _normalize_term(metric.name):
                return metric
            if any(wanted == _normalize_term(s) for s in metric.synonyms):
                return metric
        return None


def _metric(raw: dict[str, Any], source: str) -> Metric:
    return Metric(
        name=raw["name"],
        status=raw["status"],
        kind=raw["kind"],
        source=source,
        synonyms=tuple(raw.get("synonyms") or ()),
        raw=raw,
    )


def build_catalog(
    key: str,
    physical: dict[str, Any],
    semantic: dict[str, Any],
    fingerprint: tuple[tuple[str, int, int], ...] = (),
    business: dict[str, Any] | None = None,
    physical_markdown: SchemaMap | None = None,
) -> Catalog:
    """Pure parser from already-validated metadata dictionaries.

    Business metrics are merged after semantic metrics. A business metric with the same name
    replaces the semantic one in place, so accepted proposals win over generated seeds.

    `physical_markdown` is the parsed physical requirements file. When given, it must match the
    physical YAML exactly and becomes the planner's full schema. When omitted (in-memory callers
    and unit tests), the full schema is the physical YAML itself.
    """
    tables: dict[str, TableInfo] = {}
    for table in physical.get("tables", []):
        columns = {
            c["name"]: ColumnInfo(
                name=c["name"],
                type=c["type"],
                nullable=bool(c["nullable"]),
                has_nulls=bool(c["has_nulls"]),
                is_unique=bool(c["is_unique"]),
            )
            for c in table["columns"]
        }
        tables[table["name"]] = TableInfo(
            name=table["name"],
            source_sheet=table["source_sheet"],
            row_count=int(table["row_count"]),
            columns=columns,
        )
    physical_schema: SchemaMap = {
        name: {col.name: col.type for col in table.columns.values()} for name, table in tables.items()
    }
    if physical_markdown is None:
        full_schema = physical_schema
    else:
        check_against_physical(physical_markdown, physical_schema, "physical requirements")
        full_schema = physical_markdown

    relationships = tuple(
        Relationship(
            name=r["name"],
            status=r["status"],
            meaning=r["meaning"],
            cardinality=r["cardinality"],
            enabled=bool(r["enabled"]),
            join_policy=r["join_policy"],
            fan_out=r["fan_out"],
            child=_endpoint(r["child"], r["normalization"]["child"]),
            parent=_endpoint(r["parent"], r["normalization"]["parent"]),
        )
        for r in physical.get("relationships", [])
    )

    semantic_columns = tuple(
        SemanticColumn(
            table=c["table"],
            column=c["column"],
            data_type=c["data_type"],
            column_class=c["column_class"],
            synonyms=tuple(c.get("synonyms") or ()),
            values=tuple(c.get("values") or ()),
            flags=tuple(c.get("flags") or ()),
            raw=c,
        )
        for c in semantic.get("columns", [])
    )

    metrics_by_name: dict[str, Metric] = {
        raw["name"]: _metric(raw, "semantic") for raw in semantic.get("metrics") or []
    }
    for raw in (business or {}).get("metrics") or []:
        metrics_by_name[raw["name"]] = _metric(raw, "business")

    return Catalog(
        key=key,
        schema=physical["schema"],
        database_file=physical["file"],
        tables=tables,
        semantic_columns=semantic_columns,
        relationships=relationships,
        numeric_operators=tuple(semantic.get("numeric_operators") or ()),
        fingerprint=fingerprint,
        metrics=tuple(metrics_by_name.values()),
        full_schema=full_schema,
    )


def _read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"{path}: top level must be a mapping.")
    return data


def fingerprint_for(entry: DatabaseEntry) -> tuple[tuple[str, int, int], ...]:
    """Stat-only version fingerprint for the metadata files of one database.

    The business file is optional. It joins the fingerprint only while it exists, so creating
    or changing it invalidates the cache. The physical requirements file is part of the planner's
    schema, so it always joins the fingerprint when it exists.
    """
    paths = [entry.physical_mapping, entry.semantic_mapping]
    if entry.business_mapping.is_file():
        paths.append(entry.business_mapping)
    if entry.physical_requirements is not None and entry.physical_requirements.is_file():
        paths.append(entry.physical_requirements)
    fingerprint = []
    for path in paths:
        stat = path.stat()
        fingerprint.append((str(path), stat.st_mtime_ns, stat.st_size))
    return tuple(fingerprint)


def _load_business(entry: DatabaseEntry, semantic: dict[str, Any]) -> dict[str, Any] | None:
    if not entry.business_mapping.is_file():
        return None
    business = _read_yaml(entry.business_mapping)
    file = str(entry.business_mapping)
    errors: list[str] = []
    if business.get("database") != entry.key:
        errors.append(f"{file}: database is {business.get('database')!r} but registry key is {entry.key!r}.")
    errors.extend(validate_metrics(business.get("metrics") or [], file, semantic.get("columns") or []))
    if errors:
        raise MetadataMismatchError(f"{entry.key}: business metadata is invalid: " + " ".join(errors))
    return business


def load_catalog(registry: Registry, key: str) -> Catalog:
    """Validate (via `select_database`) and parse one database's metadata.

    The physical requirements file is required. Its schema must match the physical YAML.
    """
    entry = select_database(registry, key)
    physical = _read_yaml(entry.physical_mapping)
    semantic = _read_yaml(entry.semantic_mapping)
    business = _load_business(entry, semantic)
    if entry.physical_requirements is None or not entry.physical_requirements.is_file():
        raise MissingFileError(f"{key}: physical requirements file not found ({entry.physical_requirements}).")
    markdown = parse_physical_markdown(
        entry.physical_requirements.read_text(encoding="utf-8"), str(entry.physical_requirements)
    )
    return build_catalog(key, physical, semantic, fingerprint_for(entry), business, physical_markdown=markdown)


Loader = Callable[[Registry, str], Catalog]


class CatalogCache:
    """One catalog per registry key, reloaded only when that database's files change."""

    def __init__(self, loader: Loader = load_catalog) -> None:
        self._loader = loader
        self._entries: dict[str, Catalog] = {}
        self._loads: dict[str, int] = {}
        self._lock = threading.Lock()

    def get(self, registry: Registry, key: str) -> Catalog:
        with self._lock:
            entry = registry.databases.get(key)
            if entry is None or not self._stat_ok(entry):
                # Unknown key or missing file: the loader raises the precise error.
                self._entries.pop(key, None)
                return self._loader(registry, key)

            current = fingerprint_for(entry)
            cached = self._entries.get(key)
            if cached is not None and cached.fingerprint == current:
                return cached

            catalog = self._loader(registry, key)
            self._entries[key] = catalog
            self._loads[key] = self._loads.get(key, 0) + 1
            return catalog

    def load_count(self, key: str) -> int:
        return self._loads.get(key, 0)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._loads.clear()

    @staticmethod
    def _stat_ok(entry: DatabaseEntry) -> bool:
        try:
            fingerprint_for(entry)
        except OSError:
            return False
        return True

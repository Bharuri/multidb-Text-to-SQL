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
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
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

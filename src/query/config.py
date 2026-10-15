"""Typed configuration and database registry (B1).

`config.yaml` is the single source of truth. Every entry point resolves a database
through `select_database`, which fails before any LLM call when the key is unknown,
a file is missing, or metadata does not describe the registered database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from src.mapping.metadata_validator import validate_database

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config.yaml"


class ConfigError(Exception):
    """Configuration is malformed."""


class UnknownDatabaseError(ConfigError):
    """The requested database key is not registered."""


class MissingFileError(ConfigError):
    """A registered database or metadata file does not exist."""


class MetadataMismatchError(ConfigError):
    """Metadata does not describe the registered database."""


BUSINESS_DIR = Path("specs") / "properties" / "business"
PHYSICAL_REQUIREMENTS_DIR = Path("specs") / "properties" / "physical_requirements"


@dataclass(frozen=True)
class DatabaseEntry:
    key: str
    physical_mapping: Path
    semantic_mapping: Path
    database_file: Path
    database_file_setting: str
    # Optional accepted-business file (`specs/properties/business/<key>.yaml`). May not exist.
    business_mapping: Path
    # Full physical schema for the planner (`specs/properties/physical_requirements/<key>.md`).
    physical_requirements: Path | None = None


@dataclass(frozen=True)
class RetrievalLimits:
    default_row_limit: int
    max_prompt_chars: int
    value_list_threshold: int


@dataclass(frozen=True)
class LlmSettings:
    provider: str
    model_id: str
    region: str
    temperature: float
    max_output_tokens: int


@dataclass(frozen=True)
class SqlLimits:
    max_sql_chars: int
    max_ctes: int
    max_nesting_depth: int
    max_joins: int
    max_window_functions: int
    query_timeout_seconds: int


@dataclass(frozen=True)
class Registry:
    root: Path
    input_dir: Path
    database_dir: Path
    databases: dict[str, DatabaseEntry]
    retrieval: RetrievalLimits
    planner_llm: LlmSettings
    sql_generator_llm: LlmSettings
    capabilities: dict[str, bool]
    limits: SqlLimits
    logging_file: Path
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


def _require(mapping: Any, key: str, where: str) -> Any:
    if not isinstance(mapping, dict) or key not in mapping:
        raise ConfigError(f"{where}: missing required key '{key}'.")
    return mapping[key]


def _positive_int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{where}: must be a positive integer, got {value!r}.")
    return value


def _llm(section: Any, where: str) -> LlmSettings:
    temperature = _require(section, "temperature", where)
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not 0 <= temperature <= 2:
        raise ConfigError(f"{where}.temperature: must be a number between 0 and 2, got {temperature!r}.")
    return LlmSettings(
        provider=str(_require(section, "provider", where)),
        model_id=str(_require(section, "model_id", where)),
        region=str(_require(section, "region", where)),
        temperature=float(temperature),
        max_output_tokens=_positive_int(_require(section, "max_output_tokens", where), f"{where}.max_output_tokens"),
    )


def _display(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def load_registry(config_path: Path = CONFIG_PATH, root: Path = ROOT) -> Registry:
    """Parse and type-check a registry file. Does not touch database files.

    Relative paths resolve against `root` (the repository root), matching the repo-relative
    `database_file` values recorded in metadata.
    """
    config_path = Path(config_path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MissingFileError(f"{config_path}: config file not found.") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{config_path}: invalid YAML ({exc}).") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path}: top level must be a mapping.")

    databases: dict[str, DatabaseEntry] = {}
    for key, entry in (_require(raw, "databases", str(config_path)) or {}).items():
        where = f"{config_path}: databases.{key}"
        databases[key] = DatabaseEntry(
            key=key,
            physical_mapping=root / str(_require(entry, "physical_mapping", where)),
            semantic_mapping=root / str(_require(entry, "semantic_mapping", where)),
            database_file=root / str(_require(entry, "database_file", where)),
            database_file_setting=str(entry["database_file"]),
            business_mapping=root / BUSINESS_DIR / f"{key}.yaml",
            physical_requirements=root / PHYSICAL_REQUIREMENTS_DIR / f"{key}.md",
        )
    if not databases:
        raise ConfigError(f"{config_path}: 'databases' must register at least one database.")

    retrieval_raw = _require(raw, "retrieval", str(config_path))
    retrieval = RetrievalLimits(
        default_row_limit=_positive_int(_require(retrieval_raw, "default_row_limit", "retrieval"), "retrieval.default_row_limit"),
        max_prompt_chars=_positive_int(_require(retrieval_raw, "max_prompt_chars", "retrieval"), "retrieval.max_prompt_chars"),
        value_list_threshold=_positive_int(_require(retrieval_raw, "value_list_threshold", "retrieval"), "retrieval.value_list_threshold"),
    )
    capabilities = {str(k): bool(v) for k, v in (_require(raw, "capabilities", str(config_path)) or {}).items()}
    limits_raw = _require(raw, "limits", str(config_path))
    limits = SqlLimits(
        max_sql_chars=_positive_int(_require(limits_raw, "max_sql_chars", "limits"), "limits.max_sql_chars"),
        max_ctes=_positive_int(_require(limits_raw, "max_ctes", "limits"), "limits.max_ctes"),
        max_nesting_depth=_positive_int(_require(limits_raw, "max_nesting_depth", "limits"), "limits.max_nesting_depth"),
        max_joins=_positive_int(_require(limits_raw, "max_joins", "limits"), "limits.max_joins"),
        max_window_functions=_positive_int(_require(limits_raw, "max_window_functions", "limits"), "limits.max_window_functions"),
        query_timeout_seconds=_positive_int(_require(limits_raw, "query_timeout_seconds", "limits"), "limits.query_timeout_seconds"),
    )
    logging_file = root / str(_require(_require(raw, "logging", str(config_path)), "file", "logging"))

    return Registry(
        root=root,
        input_dir=root / str(_require(raw, "input_dir", str(config_path))),
        database_dir=root / str(_require(raw, "database_dir", str(config_path))),
        databases=databases,
        retrieval=retrieval,
        planner_llm=_llm(_require(raw, "planner_llm", str(config_path)), "planner_llm"),
        sql_generator_llm=_llm(_require(raw, "sql_generator_llm", str(config_path)), "sql_generator_llm"),
        capabilities=capabilities,
        limits=limits,
        logging_file=logging_file,
        raw=raw,
    )


def select_database(registry: Registry, key: str) -> DatabaseEntry:
    """Return the registered entry or raise before any LLM call.

    Checks in order: the key is registered, the DuckDB file and both metadata files exist,
    and the metadata identity (database name, file, schema mapping) matches the registry.
    """
    if key not in registry.databases:
        known = ", ".join(sorted(registry.databases))
        raise UnknownDatabaseError(f"Unknown database {key!r}. Registered databases: {known}.")
    entry = registry.databases[key]

    for label, path in (
        ("DuckDB file", entry.database_file),
        ("physical metadata", entry.physical_mapping),
        ("semantic metadata", entry.semantic_mapping),
    ):
        if not path.is_file():
            raise MissingFileError(f"{key}: {label} not found at {_display(path, registry.root)}.")

    errors = validate_database(key, entry.physical_mapping, entry.semantic_mapping, entry.database_file_setting)
    if errors:
        raise MetadataMismatchError(f"{key}: metadata does not match the registry: " + " ".join(errors))
    return entry

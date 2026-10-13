"""Business terms for the selected database. Optional: a database without a business file gets none.

A business file lists concepts. Each concept names one physical column, and its name and synonyms are
extra words the matcher may use for that column. Terms are checked against the physical mapping at load time.
A term that points at two different columns is ambiguous and rejects the whole file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.query.catalog import PROJECT_ROOT, CatalogError, Resolved, load_yaml
from src.query.matcher import normalize

CONCEPT_KEYS = frozenset({"name", "table", "column", "synonyms", "description"})
DEFINITION_KEYS = frozenset({"name", "table", "column", "operator", "value", "description"})
DEFINITION_OPERATORS = ("<", "<=", ">", ">=", "=")


class BusinessError(Exception):
    """A business file is invalid. The reasons are safe to show to the user."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


@dataclass(frozen=True)
class Concept:
    name: str
    table: str
    column: str
    synonyms: tuple[str, ...] = ()
    description: str = ""


@dataclass(frozen=True)
class Definition:
    """A named row condition, such as loss = Orders.Profit < 0. The planner may turn it into a SHARE filter."""

    name: str
    table: str
    column: str
    operator: str
    value: float
    description: str = ""


@dataclass(frozen=True)
class BusinessSemantics:
    database: str
    concepts: tuple[Concept, ...] = ()
    definitions: tuple[Definition, ...] = ()

    def synonyms_for(self, table: str, column: str) -> tuple[str, ...]:
        """Every business term that points at this column, as its name and synonyms."""
        terms: list[str] = []
        for concept in self.concepts:
            if concept.table == table and concept.column == column:
                terms.append(concept.name)
                terms.extend(concept.synonyms)
        return tuple(terms)

    def prompt_text(self) -> str:
        """One line per concept and definition for the planner and SQL prompts. Empty when there are none."""
        lines = []
        for concept in self.concepts:
            line = f"- {concept.name} = {concept.table}.{concept.column}"
            if concept.synonyms:
                line += f" (also called: {', '.join(concept.synonyms)})"
            if concept.description:
                line += f": {concept.description}"
            lines.append(line)
        for definition in self.definitions:
            line = f"- {definition.name} = {definition.table}.{definition.column} {definition.operator} {definition.value}"
            if definition.description:
                line += f": {definition.description}"
            lines.append(line)
        return "\n".join(lines)


def load_business(resolved: Resolved, root: Path = PROJECT_ROOT) -> BusinessSemantics:
    """Load the business file for the selected database only. A missing file gives an empty set."""
    path = root / "specs" / "properties" / "business" / f"{resolved.database}.yaml"
    if not path.is_file():
        return BusinessSemantics(database=resolved.database)
    try:
        data = load_yaml(path)
    except CatalogError as error:
        raise BusinessError([str(error)]) from None
    return validate_business(data, resolved)


def validate_business(data: Any, resolved: Resolved) -> BusinessSemantics:
    database = resolved.database
    if not isinstance(data, dict):
        raise BusinessError([f"business file for {database} must be a mapping"])
    reasons: list[str] = []
    if data.get("database") != database:
        reasons.append(f"business file database is {data.get('database')}, expected {database}")

    columns = {(table["name"], col["name"]) for table in resolved.physical["tables"] for col in table["columns"]}
    raw_concepts = data.get("concepts") or []
    if not isinstance(raw_concepts, list):
        raise BusinessError([f"business file for {database}: concepts must be a list"])

    owner: dict[str, tuple[str, str]] = {}
    concepts: list[Concept] = []
    for index, item in enumerate(raw_concepts, start=1):
        label = f"concept {index}"
        if not isinstance(item, dict):
            reasons.append(f"{label} must be a mapping")
            continue
        extra = sorted(set(item) - CONCEPT_KEYS)
        if extra:
            reasons.append(f"{label} has unknown keys: {', '.join(extra)}")
        name, table, column = item.get("name"), item.get("table"), item.get("column")
        if not isinstance(name, str) or not name.strip():
            reasons.append(f"{label} needs a text name")
            continue
        label = f"concept '{name}'"
        if (table, column) not in columns:
            reasons.append(f"{label} points at {table}.{column}, which is not in the physical mapping")
            continue
        synonyms = item.get("synonyms") or []
        if not isinstance(synonyms, list) or not all(isinstance(s, str) and s.strip() for s in synonyms):
            reasons.append(f"{label}: synonyms must be a list of text")
            continue
        description = item.get("description") or ""
        if not isinstance(description, str):
            reasons.append(f"{label}: description must be text")
            continue

        for term in [name, *synonyms]:
            key = normalize(term)
            if key in owner and owner[key] != (table, column):
                other = ".".join(owner[key])
                reasons.append(f"term '{term}' is ambiguous: it points at both {other} and {table}.{column}")
            owner.setdefault(key, (table, column))
        concepts.append(Concept(name, table, column, tuple(synonyms), description))

    raw_definitions = data.get("definitions") or []
    if not isinstance(raw_definitions, list):
        raise BusinessError([f"business file for {database}: definitions must be a list"])
    definitions: list[Definition] = []
    seen_definitions: set[str] = set()
    for index, item in enumerate(raw_definitions, start=1):
        label = f"definition {index}"
        if not isinstance(item, dict):
            reasons.append(f"{label} must be a mapping")
            continue
        extra = sorted(set(item) - DEFINITION_KEYS)
        if extra:
            reasons.append(f"{label} has unknown keys: {', '.join(extra)}")
        name, table, column = item.get("name"), item.get("table"), item.get("column")
        if not isinstance(name, str) or not name.strip():
            reasons.append(f"{label} needs a text name")
            continue
        label = f"definition '{name}'"
        if normalize(name) in seen_definitions:
            reasons.append(f"{label} is defined more than once")
            continue
        seen_definitions.add(normalize(name))
        if (table, column) not in columns:
            reasons.append(f"{label} points at {table}.{column}, which is not in the physical mapping")
            continue
        operator, value = item.get("operator"), item.get("value")
        if operator not in DEFINITION_OPERATORS:
            reasons.append(f"{label}: operator must be one of {', '.join(DEFINITION_OPERATORS)}")
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            reasons.append(f"{label}: value must be a number")
            continue
        description = item.get("description") or ""
        if not isinstance(description, str):
            reasons.append(f"{label}: description must be text")
            continue
        definitions.append(Definition(name, table, column, operator, value, description))

    if reasons:
        raise BusinessError(reasons)
    return BusinessSemantics(database=database, concepts=tuple(concepts), definitions=tuple(definitions))

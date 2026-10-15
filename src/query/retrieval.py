"""Deterministic relevant-context retrieval (B3).

Given a catalog and a question, select the tables, columns, value lists, and relationships
that the planner or SQL generator needs. Retrieval is lexical: it matches question tokens
against physical names, synonyms, and semantic values. It never reads files and never
crosses the catalog's database boundary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.query.catalog import Catalog, Endpoint, Relationship, SemanticColumn

TOKEN_RE = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset(
    ["a", "an", "and", "are", "as", "at", "be", "by", "by", "for", "from", "how", "in", "is", "it", "me", "my", "of", "on", "or", "show", "tell", "the", "to", "what", "which", "who", "with", "give", "list", "count", "total", "many", "much", "per", "each", "all"]
)


def _singular(token: str) -> str:
    """Reduce a plural token to its singular form with a fixed suffix rule (design §4, step 5b)."""
    if len(token) <= 3 or token.isdigit():
        return token
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith(("sses", "shes", "ches", "xes", "zes")):
        return token[:-2]
    if token.endswith(("ss", "us", "is")):
        return token
    if token.endswith("s"):
        return token[:-1]
    return token


TIME_WORDS = frozenset(
    _singular(word)
    for word in [
        "year", "years", "yearly", "annual", "annually", "month", "months", "monthly",
        "quarter", "quarters", "quarterly", "week", "weeks", "weekly", "day", "days", "daily",
        "date", "dates", "time", "period", "periods", "trend", "trends", "ago", "since",
        "recent", "earliest", "latest",
    ]
)  # Generic temporal vocabulary (design §4, steps 5a and 5b). Never dataset-specific.
YEAR_TOKEN_RE = re.compile(r"^(19|20)\d{2}$")
TEMPORAL_TYPES = ("DATE", "TIMESTAMP", "TIME")


class ContextTooLargeError(ValueError):
    """Required context does not fit in the prompt budget. Required items are never dropped."""


def tokenize(text: str) -> set[str]:
    """Normalized match tokens: lowercase, singular form, stopwords removed (design §4, step 5b)."""
    tokens = set()
    for raw in TOKEN_RE.findall(text.lower()):
        token = _singular(raw)
        if raw not in STOPWORDS and token not in STOPWORDS:
            tokens.add(token)
    return tokens


def _phrase_terms(name: str) -> set[str]:
    return tokenize(name.replace("_", " "))


def time_signal_terms(question_terms: set[str]) -> set[str]:
    """Return the temporal terms in a question: generic words and four-digit years."""
    return {t for t in question_terms if t in TIME_WORDS or YEAR_TOKEN_RE.match(t)}


def _is_temporal(column_type: str, column_class: str, column_name: str) -> bool:
    """A column is temporal by physical type, semantic class, or a temporal word in its name."""
    if column_type.upper().startswith(TEMPORAL_TYPES) or column_class == "date":
        return True
    return bool(_phrase_terms(column_name) & TIME_WORDS)


@dataclass(frozen=True)
class ColumnHit:
    table: str
    column: str
    column_type: str
    column_class: str
    reason: str
    values: tuple[str, ...] = ()


@dataclass
class RetrievedContext:
    database: str
    tables: list[str] = field(default_factory=list)
    columns: list[ColumnHit] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)
    matched_terms: list[str] = field(default_factory=list)

    def to_prompt_text(self) -> str:
        lines = [f"Database: {self.database}"]
        for table in self.tables:
            lines.append(f"Table {table}:")
            for hit in self.columns:
                if hit.table != table:
                    continue
                values = f" values={list(hit.values)}" if hit.values else ""
                lines.append(f"  - {hit.column} ({hit.column_type}, {hit.column_class}){values}")
        for rel in self.relationships:
            lines.append(
                f"Relationship {rel.name}: {_endpoint_text(rel.child)} -> {_endpoint_text(rel.parent)} "
                f"({rel.cardinality}, join={rel.join_policy}). {rel.fan_out}"
            )
        return "\n".join(lines)


def _endpoint_text(endpoint: Endpoint) -> str:
    """Show a join key with its approved normalization, so the planner copies it exactly."""
    text = f"{endpoint.table}.{endpoint.column}"
    return f"{text} normalize={list(endpoint.normalize)}" if endpoint.normalize else text


def _value_terms(values: tuple[str, ...]) -> set[str]:
    return {t for val in values for t in tokenize(str(val))}


def _semantic_terms(sem: SemanticColumn) -> set[str]:
    terms = set()
    for synonym in sem.synonyms:
        terms |= tokenize(synonym)
    return terms


def _add_temporal_columns(catalog: Catalog, hits: dict[tuple[str, str], ColumnHit]) -> None:
    """Add temporal columns of the lexically selected tables (all tables when none were selected)."""
    selected = {table for table, _ in hits} or set(catalog.tables)
    for table_name in selected:
        table = catalog.tables.get(table_name)
        if table is None:
            continue
        for column in table.columns.values():
            key = (table.name, column.name)
            if key in hits:
                continue
            sem = catalog.semantic_for(table.name, column.name)
            column_class = sem.column_class if sem else "other"
            if not _is_temporal(column.type, column_class, column.name):
                continue
            hits[key] = ColumnHit(
                table=table.name,
                column=column.name,
                column_type=column.type,
                column_class=column_class,
                reason="time_signal",
            )


def retrieve(catalog: Catalog, question: str, value_list_threshold: int = 30) -> RetrievedContext:
    """Select relevant metadata for one question within one database."""
    question_terms = tokenize(question)
    context = RetrievedContext(database=catalog.key)
    matched: set[str] = set()

    hits: dict[tuple[str, str], ColumnHit] = {}
    for table in catalog.tables.values():
        for column in table.columns.values():
            column_terms = _phrase_terms(column.name)
            sem = catalog.semantic_for(table.name, column.name)
            sem_terms = _semantic_terms(sem) if sem else set()
            overlap = question_terms & (column_terms | sem_terms)
            if overlap:
                matched |= overlap
                reason = ",".join(sorted(overlap))
            else:
                reason = ""
            values: tuple[str, ...] = ()
            if sem and sem.values and len(sem.values) <= value_list_threshold:
                value_terms = _value_terms(sem.values)
                value_overlap = question_terms & value_terms
                if value_overlap:
                    matched |= value_overlap
                    values = sem.values
                    reason = ",".join(sorted(overlap | value_overlap))
            if not reason:
                continue
            hits[(table.name, column.name)] = ColumnHit(
                table=table.name,
                column=column.name,
                column_type=column.type,
                column_class=sem.column_class if sem else "other",
                reason=reason,
                values=values,
            )
    time_terms = time_signal_terms(question_terms)
    if time_terms:
        matched |= time_terms
        _add_temporal_columns(catalog, hits)
    context.columns = sorted(hits.values(), key=lambda h: (h.table, h.column))
    context.tables = sorted({h.table for h in context.columns})
    context.matched_terms = sorted(matched)

    # Minimal relationship context: enabled relationships whose both endpoints are selected.
    selected = set(context.tables)
    context.relationships = [
        r
        for r in catalog.enabled_relationships
        if r.child.table in selected and r.parent.table in selected
    ]
    return context


def enforce_budget(context: RetrievedContext, max_chars: int) -> RetrievedContext:
    """Raise if the rendered context is too large. Never truncate required items."""
    size = len(context.to_prompt_text())
    if size > max_chars:
        raise ContextTooLargeError(
            f"Retrieved context for {context.database} is {size} chars, over the {max_chars} limit."
        )
    return context

"""Metric resolution, proposals for undefined terms, and saving accepted proposals.

A term resolves to `approved` (usable in SQL), `proposed` (needs user confirmation), or
`undefined` (a proposal may be generated). Only accepted proposals are written, with status
`approved`, to the database's business file. Rejected proposals are discarded.
"""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from src.mapping.metadata_validator import METRIC_KIND_KEYS, validate_metrics
from src.query.catalog import Catalog, Metric, _normalize_term
from src.query.config import DatabaseEntry, LlmSettings
from src.query.llm import Credentials, boto_session

APPROVED = "approved"
PROPOSED = "proposed"
UNDEFINED = "undefined"

Proposer = Callable[[str], str]


class MetricError(Exception):
    """A metric cannot be used or saved."""


class ProposalError(MetricError):
    """The model's proposal could not be parsed or failed metric validation."""


@dataclass(frozen=True)
class Resolution:
    term: str
    outcome: str
    metric: Metric | None


def resolve_term(catalog: Catalog, term: str) -> Resolution:
    metric = catalog.metric_for(term)
    if metric is None:
        return Resolution(term, UNDEFINED, None)
    return Resolution(term, metric.status, metric)


def build_proposal_prompt(catalog: Catalog, term: str, question: str) -> str:
    columns = [
        {
            "table": c.table,
            "column": c.column,
            "data_type": c.data_type,
            "column_class": c.column_class,
            "synonyms": list(c.synonyms),
        }
        for c in catalog.semantic_columns
    ]
    kinds = {kind: sorted(keys) for kind, keys in METRIC_KIND_KEYS.items()}
    return (
        "You define one business metric for a text-to-SQL system. Reply with a single JSON object and nothing else.\n"
        f"Metric term to define: {term!r}\n"
        f"User question: {question!r}\n"
        f"Database: {catalog.key}\n"
        f"Columns (use only these table and column names): {json.dumps(columns)}\n"
        f"Allowed kinds and their required keys: {json.dumps(kinds)}\n"
        "Aggregate specs are {table, column, aggregate, filters?}; aggregate is one of sum, count, avg. "
        "Filters are {column, operator, value} on the same table; operator is one of = != < <= > >=. "
        "Quote text values that look like booleans, such as \"Yes\".\n"
        "Required keys: name (snake_case), synonyms (list), kind, unit, grain, null_zero_policy, description, "
        "plus the keys for the chosen kind."
    )


def _parse_definition(raw: str) -> dict[str, Any]:
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        raise ProposalError("the model reply did not contain a JSON object.")
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ProposalError(f"the model reply was not valid JSON ({exc.msg}).") from exc
    if not isinstance(data, dict):
        raise ProposalError("the model reply JSON is not an object.")
    return data


def propose_metric(catalog: Catalog, term: str, question: str, proposer: Proposer) -> dict[str, Any]:
    """Ask the proposer for a definition of an undefined term. The result is validated and marked `proposed`."""
    if catalog.metric_for(term) is not None:
        raise MetricError(f"metric {term!r} is already defined; no proposal is needed.")
    definition = _parse_definition(proposer(build_proposal_prompt(catalog, term, question)))
    definition["name"] = _normalize_term(term)
    definition["status"] = PROPOSED
    synonyms = [str(s) for s in definition.get("synonyms") or []]
    if term.strip() and term.strip() not in synonyms:
        synonyms.append(term.strip())
    definition["synonyms"] = synonyms
    errors = validate_metrics(
        [definition], f"proposal:{catalog.key}", [c.raw for c in catalog.semantic_columns]
    )
    if errors:
        raise ProposalError(" ".join(errors))
    return definition


def _read_business(entry: DatabaseEntry) -> dict[str, Any]:
    if not entry.business_mapping.exists():
        return {"database": entry.key, "metrics": []}
    data = yaml.safe_load(entry.business_mapping.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise MetricError(f"{entry.business_mapping}: top level must be a mapping.")
    if data.get("database") != entry.key:
        raise MetricError(
            f"{entry.business_mapping}: database is {data.get('database')!r} but registry key is {entry.key!r}."
        )
    return data


def _write_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temp, path)


def accept_proposal(catalog: Catalog, entry: DatabaseEntry, definition: dict[str, Any]) -> Path:
    """Save a confirmed proposal as `approved` in the business file, keeping every other key."""
    if definition.get("status") != PROPOSED:
        raise MetricError("only a proposed metric can be accepted.")
    accepted = copy.deepcopy(definition)
    accepted["status"] = APPROVED

    data = _read_business(entry)
    metrics = [m for m in data.get("metrics") or [] if m.get("name") != accepted["name"]]
    metrics.append(accepted)
    errors = validate_metrics(metrics, str(entry.business_mapping), [c.raw for c in catalog.semantic_columns])
    if errors:
        raise MetricError(" ".join(errors))
    data["metrics"] = metrics
    _write_atomic(entry.business_mapping, data)
    return entry.business_mapping


def bedrock_proposer(
    settings: LlmSettings,
    profile_name: str | None = None,
    credentials: Credentials | None = None,
) -> Proposer:
    """Build a proposer backed by Bedrock Converse. Explicit keys win; otherwise the AWS profile chain."""
    session = boto_session(settings.region, profile_name, credentials)
    client = session.client("bedrock-runtime")

    def propose(prompt: str) -> str:
        response = client.converse(
            modelId=settings.model_id,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"temperature": settings.temperature, "maxTokens": settings.max_output_tokens},
        )
        blocks = response["output"]["message"]["content"]
        return "".join(block.get("text", "") for block in blocks)

    return propose

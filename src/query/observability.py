"""Query observability models (B5).

`StageResult` and `Response` are the single shape shared by the trace, the UI, and the
logs. Output and reason strings are bounded so metadata or results can never flood the
trace. Nothing here stores credentials, prompts, or raw model output.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

StageStatus = Literal["ok", "failed", "skipped"]
ResponseStatus = Literal["ok", "needs_clarification", "cannot_answer", "rejected", "error"]

FAILURE_CATEGORIES = frozenset(
    {
        "invalid_request",
        "unknown_database",
        "metadata_error",
        "clarification_needed",
        "unsupported",
        "llm_error",
        "invalid_plan",
        "invalid_sql",
        "unsafe_sql",
        "execution_error",
        "internal_error",
    }
)

MAX_OUTPUT_CHARS = 2000
MAX_REASON_CHARS = 500
MAX_DETAIL_CHARS = 20000
_TRUNCATED = "...[truncated]"


def _bounded(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(_TRUNCATED)] + _TRUNCATED


def _bounded_output(output: dict[str, Any]) -> dict[str, Any]:
    if not output:
        return {}
    text = json.dumps(output, default=str, sort_keys=True)
    if len(text) <= MAX_OUTPUT_CHARS:
        return dict(output)
    return {"summary": _bounded(text, MAX_OUTPUT_CHARS)}


def _json_default(value: Any) -> Any:
    if isinstance(value, (set, frozenset)):
        return sorted(str(item) for item in value)
    return str(value)


def bounded_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """JSON-safe UI detail. Each text value is bounded; structured values are small by construction."""
    bounded: dict[str, Any] = {}
    for key, value in detail.items():
        if isinstance(value, str):
            bounded[key] = _bounded(value, MAX_DETAIL_CHARS)
        else:
            bounded[key] = json.loads(json.dumps(value, default=_json_default))
    return bounded


@dataclass(frozen=True)
class StageResult:
    stage: str
    status: StageStatus
    output: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    duration_ms: float = 0.0
    category: str | None = None
    # UI-only detail: prompts, raw LLM replies, the parsed plan, SQL, and preview rows. It is never
    # logged and never part of to_dict(), so the serialized trace and logs stay summaries.
    detail: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.status not in ("ok", "failed", "skipped"):
            raise ValueError(f"invalid stage status: {self.status!r}")
        if self.status == "failed" and self.category not in FAILURE_CATEGORIES:
            raise ValueError(f"failed stage needs a known category, got {self.category!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "status": self.status,
            "output": _bounded_output(self.output),
            "reason": _bounded(self.reason, MAX_REASON_CHARS) if self.reason else None,
            "duration_ms": round(self.duration_ms, 3),
            "category": self.category,
        }


@dataclass(frozen=True)
class Response:
    status: ResponseStatus
    trace: tuple[StageResult, ...] = ()
    result: dict[str, Any] = field(default_factory=dict)
    sql: str | None = None
    params: tuple[Any, ...] = ()
    reasons: tuple[str, ...] = ()
    category: str | None = None

    def __post_init__(self) -> None:
        if self.status != "ok" and self.category not in FAILURE_CATEGORIES:
            raise ValueError(f"non-ok response needs a known category, got {self.category!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "category": self.category,
            "trace": [s.to_dict() for s in self.trace],
            "result": self.result,
            "sql": self.sql,
            "params": list(self.params),
            "reasons": [_bounded(r, MAX_REASON_CHARS) for r in self.reasons],
        }


def skipped_after(stages: list[str], failed_stage: str) -> list[StageResult]:
    """Mark every stage after `failed_stage` as skipped, keeping stage order."""
    index = stages.index(failed_stage)
    return [StageResult(stage=name, status="skipped") for name in stages[index + 1 :]]


LOGGER = logging.getLogger("multidb.query")

# Output keys that are safe to log: counts and fixed-vocabulary flag names. Never prompts, SQL, rows, or values.
LOGGED_OUTPUT_KEYS = ("attempts", "characters", "features", "joins", "parameters", "row_count", "truncated")


def log_stage(result: StageResult, request_id: str | None = None) -> None:
    """One structured, privacy-safe record per stage. No output body, prompts, or secrets."""
    fields = {
        "request_id": request_id,
        "stage": result.stage,
        "status": result.status,
        "duration_ms": round(result.duration_ms, 3),
        "category": result.category,
    }
    counters = {key: result.output[key] for key in LOGGED_OUTPUT_KEYS if key in result.output}
    if counters:
        fields["counters"] = counters
    LOGGER.info("stage %s", json.dumps(fields, sort_keys=True, default=str))

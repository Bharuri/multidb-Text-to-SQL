"""Planner reply parser.

The planner returns a short JSON object with a status and a plain-text analysis. The analysis is
passed to the SQL generator as written. This module checks only the envelope: the status, that a
ready reply has an analysis, and that a non-ready reply explains why. It does not inspect the
analysis text.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

STATUSES = frozenset({"ready", "needs_clarification", "cannot_answer"})
MAX_ANALYSIS_CHARS = 8000
MAX_MISSING_TERMS = 10


class AnalysisError(Exception):
    """Raised when the planner reply does not match the analysis envelope."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


@dataclass(frozen=True)
class Clarification:
    message: str
    missing: tuple[str, ...] = ()


@dataclass(frozen=True)
class Analysis:
    status: str
    analysis: str
    clarification: Clarification | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


def _json_object(text: str) -> dict[str, Any]:
    if not isinstance(text, str) or not text.strip():
        raise AnalysisError("$", "the reply is empty")
    body = text.strip()
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise AnalysisError("$", "the reply has no JSON object")
    try:
        value = json.loads(body[start : end + 1])
    except json.JSONDecodeError as exc:
        raise AnalysisError("$", f"the reply is not valid JSON (line {exc.lineno})") from None
    if not isinstance(value, dict):
        raise AnalysisError("$", "the reply must be a JSON object")
    return value


def _clarification(value: Any) -> Clarification | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise AnalysisError("clarification", "must be an object or null")
    message = value.get("message")
    if not isinstance(message, str) or not message.strip():
        raise AnalysisError("clarification.message", "must be a non-empty string")
    missing = value.get("missing", [])
    if not isinstance(missing, list) or not all(isinstance(term, str) for term in missing):
        raise AnalysisError("clarification.missing", "must be a list of strings")
    return Clarification(message.strip(), tuple(missing[:MAX_MISSING_TERMS]))


def parse_analysis(text: str) -> Analysis:
    """Parse one planner reply. Raises AnalysisError when the envelope is wrong."""
    value = _json_object(text)
    status = value.get("status")
    if status not in STATUSES:
        raise AnalysisError("status", f"must be one of {sorted(STATUSES)}")
    clarification = _clarification(value.get("clarification"))
    analysis = value.get("analysis", "")
    if not isinstance(analysis, str):
        raise AnalysisError("analysis", "must be a string")
    analysis = analysis.strip()
    if len(analysis) > MAX_ANALYSIS_CHARS:
        raise AnalysisError("analysis", f"is longer than {MAX_ANALYSIS_CHARS} characters")
    if status == "ready":
        if not analysis:
            raise AnalysisError("analysis", "a ready reply needs the analysis")
        clarification = None
    elif clarification is None:
        raise AnalysisError("clarification", f"a {status} reply needs a message explaining why")
    return Analysis(status=status, analysis=analysis, clarification=clarification, raw=value)

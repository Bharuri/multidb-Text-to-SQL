"""SQL Generator client (D2).

The SQL Generator is a separate model call from the planner. It has its own settings
block, so model and limits can differ. The raw text is returned for D3 to parse. This
module does not parse or validate SQL.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from src.query.config import Registry
from src.query.llm import Credentials, LlmClient, LlmError, make_client
from src.query.observability import StageResult

SQL_GENERATE_STAGE = "sql_generate"


def build_sql_generator(
    registry: Registry,
    profile_name: str | None = None,
    credentials: Credentials | None = None,
) -> LlmClient:
    return make_client(registry.sql_generator_llm, profile_name=profile_name, credentials=credentials)


def generate_sql(
    client: LlmClient,
    prompt: str,
    on_chunk: Callable[[str], None] | None = None,
) -> tuple[str, StageResult]:
    started = time.monotonic()
    try:
        text = client.complete(prompt, on_chunk=on_chunk)
    except LlmError as exc:
        return "", _failed(started, str(exc))
    output: dict[str, Any] = {"characters": len(text)}
    return text, StageResult(
        stage=SQL_GENERATE_STAGE,
        status="ok",
        output=output,
        duration_ms=_elapsed_ms(started),
    )


def _failed(started: float, reason: str) -> StageResult:
    return StageResult(
        stage=SQL_GENERATE_STAGE,
        status="failed",
        reason=reason or "SQL generator call failed",
        duration_ms=_elapsed_ms(started),
        category="llm_error",
    )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)

"""Query pipeline orchestrator (E2).

Runs the stages in order. The first failed stage stops the run, later stages are recorded as
`skipped`, and the result is a `Response`. Each step returns a small output dict or raises
`_StageFailure` with a design section 9.1 category. Unexpected exceptions become
`internal_error` with only the exception type name in the reason.

The planner writes a plain-text analysis inside a small JSON envelope. The SQL generator reads
that analysis and writes free SQL. Deterministic checks then cover the SQL: AST shape, limits,
column names, and listed literal values.

LLM clients can be passed in for tests. Otherwise the planner and SQL generator are built
from the registry settings.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from src.query.analysis import Analysis, AnalysisError, parse_analysis
from src.query.catalog import Catalog, load_catalog
from src.query.config import (
    ConfigError,
    DatabaseEntry,
    Registry,
    load_registry,
    select_database,
)
from src.query.executor import ExecutionError, ExecutionResult, execute_read_only
from src.query.llm import LlmClient, LlmError, make_client
from src.query.observability import (
    Response,
    StageResult,
    bounded_detail,
    log_stage,
    skipped_after,
)
from src.query.prompts import (
    build_planner_prompt,
    build_repair_prompt,
    build_sql_prompt,
    build_sql_repair_prompt,
)
from src.query.retrieval import (
    ContextTooLargeError,
    RetrievedContext,
    enforce_budget,
    retrieve,
)
from src.query.sql_ast import AstSummary, SqlAstError, validate_sql_ast
from src.query.sql_checks import check_sql_names_and_values, rewrite_column_names
from src.query.sql_generator import build_sql_generator, generate_sql
from src.query.sql_limits import check_sql_limits
from src.query.sql_parser import GeneratedSql, SqlParseError, parse_generated_sql

RESPONSE_STATUS_BY_CATEGORY = {
    "clarification_needed": "needs_clarification",
    "unsupported": "cannot_answer",
    "invalid_plan": "rejected",
    "invalid_sql": "rejected",
    "unsafe_sql": "rejected",
    "invalid_request": "error",
    "unknown_database": "error",
    "metadata_error": "error",
    "llm_error": "error",
    "execution_error": "error",
    "internal_error": "error",
}


class _StageFailure(Exception):
    def __init__(self, category: str, reason: str, output: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.category = category
        self.reason = reason
        self.output = output or {}


def _fail(category: str, reason: str, output: dict[str, Any] | None = None) -> _StageFailure:
    return _StageFailure(category, reason, output)


def _described(exc: Exception) -> str:
    """Path and reason from the validators. Messages carry field paths, not row data."""
    return f"{exc.path}: {exc.reason}"  # type: ignore[attr-defined]


PREVIEW_ROWS = 50
PREVIEW_CELL_CHARS = 200


def _preview_rows(rows: list[list[Any]]) -> list[list[Any]]:
    """First rows for the UI, with long cell text cut short."""
    preview = []
    for row in rows[:PREVIEW_ROWS]:
        preview.append(
            [cell[:PREVIEW_CELL_CHARS] if isinstance(cell, str) else cell for cell in row]
        )
    return preview


@dataclass
class _Context:
    question: str
    database_key: str
    registry: Registry | None
    planner: LlmClient | None
    sql_generator: LlmClient | None
    entry: DatabaseEntry | None = None
    catalog: Catalog | None = None
    retrieved: RetrievedContext | None = None
    planner_prompt: str = ""
    planner_text: str = ""
    analysis: Analysis | None = None
    sql_prompt: str = ""
    generator_text: str = ""
    generated: GeneratedSql | None = None
    summary: AstSummary | None = None
    execution: ExecutionResult | None = None
    # Per-stage UI detail. _run_stage clears it before each step and copies it into the StageResult.
    detail: dict[str, Any] = field(default_factory=dict)


def _submit(ctx: _Context) -> dict[str, Any]:
    question = ctx.question.strip() if isinstance(ctx.question, str) else ""
    if not question:
        raise _fail("invalid_request", "The question is empty.")
    if not isinstance(ctx.database_key, str) or not ctx.database_key.strip():
        raise _fail("invalid_request", "No database was selected.")
    ctx.question = question
    ctx.detail["question"] = question
    return {"question_characters": len(question)}


def _resolve_database(ctx: _Context) -> dict[str, Any]:
    if ctx.registry is None:
        try:
            ctx.registry = load_registry()
        except ConfigError as exc:
            raise _fail("metadata_error", str(exc)) from exc
    try:
        ctx.entry = select_database(ctx.registry, ctx.database_key.strip())
    except ConfigError as exc:
        category = "unknown_database" if type(exc).__name__ == "UnknownDatabaseError" else "metadata_error"
        raise _fail(category, str(exc)) from exc
    ctx.detail["database"] = ctx.entry.key
    ctx.detail["database_file"] = Path(str(ctx.entry.database_file)).name
    return {"database": ctx.entry.key}


def _load_catalog(ctx: _Context) -> dict[str, Any]:
    try:
        ctx.catalog = load_catalog(ctx.registry, ctx.entry.key)
    except ConfigError as exc:
        raise _fail("metadata_error", str(exc)) from exc
    ctx.detail["tables"] = sorted(ctx.catalog.tables)
    ctx.detail["metrics"] = [metric.name for metric in ctx.catalog.metrics]
    return {"tables": len(ctx.catalog.tables), "metrics": len(ctx.catalog.metrics)}


def _retrieve_context(ctx: _Context) -> dict[str, Any]:
    ctx.retrieved = retrieve(
        ctx.catalog,
        ctx.question,
        value_list_threshold=ctx.registry.retrieval.value_list_threshold,
    )
    try:
        enforce_budget(ctx.retrieved, ctx.registry.retrieval.max_prompt_chars)
    except ContextTooLargeError as exc:
        raise _fail("unsupported", str(exc)) from exc
    ctx.detail["retrieved_context"] = ctx.retrieved.to_prompt_text()
    return {"characters": len(ctx.retrieved.to_prompt_text())}


def _build_planner_prompt(ctx: _Context) -> dict[str, Any]:
    prompt = build_planner_prompt(
        ctx.question, ctx.retrieved, ctx.catalog.metrics, full_schema=ctx.catalog.full_schema
    )
    ctx.planner_prompt = prompt
    ctx.detail["planner_prompt"] = prompt
    limit = ctx.registry.retrieval.max_prompt_chars
    if len(prompt) > limit:
        raise _fail("unsupported", f"The planner prompt is {len(prompt)} characters, over the {limit} limit.")
    return {"characters": len(prompt)}


def _planner_reply(ctx: _Context, prompt: str) -> str:
    try:
        client = ctx.planner if ctx.planner is not None else make_client(ctx.registry.planner_llm)
        return client.complete(prompt)
    except LlmError as exc:
        raise _fail("llm_error", str(exc) or "The planner call failed.") from exc


def _envelope_rejection(text: str) -> str | None:
    """Return the reason when a planner reply does not match the envelope, so one repair call can fix it."""
    try:
        parse_analysis(text)
    except AnalysisError as exc:
        return _described(exc)
    return None


def _planner_llm(ctx: _Context) -> dict[str, Any]:
    text = _planner_reply(ctx, ctx.planner_prompt)
    attempts = 1
    reason = _envelope_rejection(text)
    if reason is not None:
        ctx.detail["first_reply"] = text
        ctx.detail["repair_reason"] = reason
        repair_prompt = build_repair_prompt(ctx.planner_prompt, text, reason)
        if len(repair_prompt) <= ctx.registry.retrieval.max_prompt_chars:
            text = _planner_reply(ctx, repair_prompt)
            attempts = 2
    ctx.planner_text = text
    ctx.detail["planner_reply"] = text
    return {"characters": len(text), "attempts": attempts}


def _parse_analysis(ctx: _Context) -> dict[str, Any]:
    try:
        analysis = parse_analysis(ctx.planner_text)
    except AnalysisError as exc:
        raise _fail("invalid_plan", _described(exc)) from exc
    ctx.analysis = analysis
    if analysis.status != "ready":
        # parse_analysis requires a message for every non-ready reply.
        category = "clarification_needed" if analysis.status == "needs_clarification" else "unsupported"
        missing = list(analysis.clarification.missing)
        ctx.detail["clarification"] = asdict(analysis.clarification)
        raise _fail(category, analysis.clarification.message, {"status": analysis.status, "missing": missing})
    ctx.detail["analysis"] = analysis.analysis
    return {"status": analysis.status, "characters": len(analysis.analysis)}


def _build_sql_prompt(ctx: _Context) -> dict[str, Any]:
    ctx.sql_prompt = build_sql_prompt(
        ctx.question,
        ctx.analysis.analysis,
        ctx.catalog,
        ctx.retrieved,
        ctx.registry.capabilities,
    )
    ctx.detail["sql_prompt"] = ctx.sql_prompt
    return {"characters": len(ctx.sql_prompt)}


def _sql_generator_llm(ctx: _Context) -> dict[str, Any]:
    try:
        client = ctx.sql_generator if ctx.sql_generator is not None else build_sql_generator(ctx.registry)
    except LlmError as exc:
        raise _fail("llm_error", str(exc) or "The SQL generator call failed.") from exc
    text, result = generate_sql(client, ctx.sql_prompt)
    ctx.generator_text = text
    ctx.detail["generator_reply"] = text
    if result.status == "failed":
        raise _fail(result.category or "llm_error", result.reason or "The SQL generator call failed.")
    return {"characters": len(text)}


def _parse_generated_sql(ctx: _Context) -> dict[str, Any]:
    attempts = 1
    try:
        ctx.generated = parse_generated_sql(ctx.generator_text)
    except SqlParseError as exc:
        reason = _described(exc)
        ctx.detail["generator_reply"] = ctx.generator_text
        ctx.detail["repair_reason"] = reason
        ctx.generator_text = _generate_repaired_sql(ctx, reason, "invalid_sql")
        attempts = 2
        try:
            ctx.generated = parse_generated_sql(ctx.generator_text)
        except SqlParseError as second:
            ctx.detail["generator_reply"] = ctx.generator_text
            raise _fail("invalid_sql", _described(second)) from second
    ctx.detail["generator_reply"] = ctx.generator_text
    ctx.detail["sql"] = ctx.generated.sql
    ctx.detail["params"] = list(ctx.generated.params)
    return {"parameters": len(ctx.generated.params), "attempts": attempts}


def _check_generated_sql(ctx: _Context) -> AstSummary:
    rewritten, rewrites = rewrite_column_names(ctx.generated.sql, ctx.catalog)
    if rewrites:
        ctx.generated = replace(ctx.generated, sql=rewritten)
        ctx.detail["sql"] = rewritten
        ctx.detail["column_rewrites"] = [{"written": w, "physical": p} for w, p in rewrites]
    generated = ctx.generated
    summary = validate_sql_ast(
        generated.sql,
        ctx.catalog,
        len(generated.params),
        ctx.registry.capabilities,
    )
    check_sql_limits(
        generated.sql,
        summary,
        ctx.registry.limits,
        ctx.registry.retrieval.default_row_limit,
    )
    check_sql_names_and_values(generated.sql, generated.params, ctx.catalog)
    return summary


def _repairable_sql_rejection(exc: SqlAstError) -> bool:
    """One repair call covers SQL the model can rewrite: an unsupported construct, an invalid
    name or structure, a disallowed function, a LIMIT or OFFSET outside its bound, or a reply with
    several statements. Writes and tables outside the dataset are never repaired."""
    if exc.category in ("unsupported", "invalid_sql", "from", "joins"):
        return True
    return exc.category == "unsafe_sql" and (
        "not allowed" in exc.reason or "exactly one SQL statement" in exc.reason
    )


def _validate_sql(ctx: _Context) -> dict[str, Any]:
    ctx.detail["sql"] = ctx.generated.sql
    attempts = 1
    try:
        summary = _check_generated_sql(ctx)
    except SqlAstError as exc:
        if not _repairable_sql_rejection(exc):
            raise _fail(exc.category, _described(exc)) from exc
        summary = _repair_generated_sql(ctx, _described(exc), exc.category)
        attempts = 2
    ctx.summary = summary
    ctx.detail["ast_summary"] = asdict(summary)
    ctx.detail["sql_attempts"] = attempts
    return {
        "tables": list(summary.tables),
        "features": list(summary.features),
        "joins": summary.join_count,
        "attempts": attempts,
    }


def _generate_repaired_sql(ctx: _Context, reason: str, category: str) -> str:
    """Send one SQL repair call for a rejected reply and return the new reply text."""
    repair_prompt = build_sql_repair_prompt(ctx.sql_prompt, ctx.generator_text, reason)
    if len(repair_prompt) > ctx.registry.retrieval.max_prompt_chars:
        raise _fail(category, reason)
    client = ctx.sql_generator if ctx.sql_generator is not None else build_sql_generator(ctx.registry)
    text, result = generate_sql(client, repair_prompt)
    if result.status == "failed":
        raise _fail(result.category or "llm_error", result.reason or "The SQL generator call failed.")
    return text


def _repair_generated_sql(ctx: _Context, reason: str, category: str) -> AstSummary:
    """Send one repair call for rewritable SQL, then validate the new reply once."""
    ctx.detail["first_sql"] = ctx.generated.sql
    ctx.detail["repair_reason"] = reason
    text = _generate_repaired_sql(ctx, reason, category)
    try:
        generated = parse_generated_sql(text)
    except SqlParseError as exc:
        ctx.detail["generator_reply"] = text
        raise _fail("invalid_sql", _described(exc)) from exc
    ctx.generator_text = text
    ctx.generated = generated
    ctx.detail["generator_reply"] = text
    ctx.detail["sql"] = generated.sql
    ctx.detail["params"] = list(generated.params)
    try:
        return _check_generated_sql(ctx)
    except SqlAstError as exc:
        raise _fail(exc.category, _described(exc)) from exc


def _run_generated_sql(ctx: _Context) -> ExecutionResult:
    generated = ctx.generated
    return execute_read_only(
        ctx.entry.database_file,
        generated.sql,
        generated.params,
        max_rows=ctx.registry.retrieval.default_row_limit,
        timeout_seconds=ctx.registry.limits.query_timeout_seconds,
        schema=ctx.catalog.schema,
    )


def _execute(ctx: _Context) -> dict[str, Any]:
    try:
        ctx.execution = _run_generated_sql(ctx)
    except ExecutionError as exc:
        # A runtime error with a database message gets one repair; the message is sent to the model
        # but never shown to the user. Timeouts and other failures without a message are not repaired.
        if not exc.diagnostic or ctx.detail.get("sql_attempts", 1) > 1:
            raise _fail("execution_error", exc.reason) from exc
        ctx.detail["execution_diagnostic"] = exc.diagnostic
        ctx.summary = _repair_generated_sql(ctx, f"{exc.reason}: {exc.diagnostic}", "execution_error")
        ctx.detail["ast_summary"] = asdict(ctx.summary)
        ctx.detail["sql_attempts"] = 2
        try:
            ctx.execution = _run_generated_sql(ctx)
        except ExecutionError as second:
            raise _fail("execution_error", second.reason) from second
    ctx.detail["columns"] = list(ctx.execution.columns)
    ctx.detail["preview_rows"] = _preview_rows(ctx.execution.rows)
    ctx.detail["preview_row_limit"] = PREVIEW_ROWS
    return {
        "row_count": ctx.execution.row_count,
        "truncated": ctx.execution.truncated,
        "columns": len(ctx.execution.columns),
    }


def _format_response(ctx: _Context) -> dict[str, Any]:
    return {"row_count": ctx.execution.row_count}


_STEPS: tuple[tuple[str, Callable[[_Context], dict[str, Any]]], ...] = (
    ("submit", _submit),
    ("resolve_database", _resolve_database),
    ("load_catalog", _load_catalog),
    ("retrieve_context", _retrieve_context),
    ("build_planner_prompt", _build_planner_prompt),
    ("planner_llm", _planner_llm),
    ("parse_analysis", _parse_analysis),
    ("build_sql_prompt", _build_sql_prompt),
    ("sql_generator_llm", _sql_generator_llm),
    ("parse_generated_sql", _parse_generated_sql),
    ("validate_sql", _validate_sql),
    ("execute", _execute),
    ("format_response", _format_response),
)

STAGES: tuple[str, ...] = tuple(name for name, _ in _STEPS)


def _elapsed_ms(started: float) -> float:
    return (time.monotonic() - started) * 1000


def _run_stage(name: str, step: Callable[[_Context], dict[str, Any]], ctx: _Context) -> StageResult:
    started = time.monotonic()
    ctx.detail.clear()
    try:
        output = step(ctx)
    except _StageFailure as exc:
        return StageResult(
            stage=name,
            status="failed",
            output=exc.output,
            reason=exc.reason,
            duration_ms=_elapsed_ms(started),
            category=exc.category,
            detail=bounded_detail(ctx.detail),
        )
    except Exception as exc:  # noqa: BLE001 - unexpected: keep the message out of the trace
        return StageResult(
            stage=name,
            status="failed",
            reason=f"Unexpected internal error ({type(exc).__name__}).",
            duration_ms=_elapsed_ms(started),
            category="internal_error",
            detail=bounded_detail(ctx.detail),
        )
    return StageResult(
        stage=name,
        status="ok",
        output=output,
        duration_ms=_elapsed_ms(started),
        detail=bounded_detail(ctx.detail),
    )


def _failure_response(ctx: _Context, trace: list[StageResult], failed: StageResult) -> Response:
    generated = ctx.generated
    return Response(
        status=RESPONSE_STATUS_BY_CATEGORY[failed.category],
        trace=tuple(trace),
        sql=generated.sql if generated else None,
        params=tuple(generated.params) if generated else (),
        reasons=(failed.reason,) if failed.reason else (),
        category=failed.category,
    )


def _success_response(ctx: _Context, trace: list[StageResult]) -> Response:
    execution = ctx.execution
    return Response(
        status="ok",
        trace=tuple(trace),
        result={
            "columns": list(execution.columns),
            "rows": execution.rows,
            "row_count": execution.row_count,
            "truncated": execution.truncated,
        },
        sql=ctx.generated.sql,
        params=tuple(ctx.generated.params),
    )


def run_pipeline(
    question: str,
    database_key: str,
    *,
    registry: Registry | None = None,
    planner: LlmClient | None = None,
    sql_generator: LlmClient | None = None,
    request_id: str | None = None,
    on_stage: Callable[[StageResult], None] | None = None,
) -> Response:
    """Answer one question against one registered database. Never raises for expected failures.

    ``on_stage`` receives each StageResult as soon as it is final, including skipped stages.
    """
    ctx = _Context(
        question=question,
        database_key=database_key,
        registry=registry,
        planner=planner,
        sql_generator=sql_generator,
    )
    trace: list[StageResult] = []

    def emit(result: StageResult) -> None:
        trace.append(result)
        log_stage(result, request_id)
        if on_stage is not None:
            on_stage(result)

    for name, step in _STEPS:
        result = _run_stage(name, step, ctx)
        emit(result)
        if result.status == "failed":
            for skipped in skipped_after(list(STAGES), name):
                emit(skipped)
            return _failure_response(ctx, trace, result)
    return _success_response(ctx, trace)

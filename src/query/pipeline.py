"""Ten-stage question pipeline. Stages are replaced one at a time by their real modules; the rest stay stubs."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from src.query.catalog import PROJECT_ROOT, CatalogError, load_yaml, resolve
from src.query.executor import ExecuteError, execute
from src.query.llm import LlmError, complete
from src.query.lookup import make_lookup
from src.query.matcher import MatchError, match_plan
from src.query.plan import CannotAnswer, PlanError, parse_reply
from src.query.prompt import PromptError, build_prompt
from src.query.sql_builder import BuildError, build_sql
from src.query.validator import validate

STAGE_NAMES = ("submit", "resolve", "prompt", "llm", "plan", "match", "build_sql", "validate", "execute", "respond")
LOGGER = logging.getLogger("multidb.query")


@dataclass(frozen=True)
class Credentials:
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    session_token: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Request:
    database: str
    question: str
    credentials: Credentials | None = field(default=None, repr=False)
    on_chunk: Callable[[str], None] | None = field(default=None, repr=False, compare=False)


STAGE_STATUSES = ("ok", "failed", "skipped")
RESPONSE_STATUSES = ("ok", "rejected", "cannot_answer", "error")


@dataclass(frozen=True)
class StageResult:
    stage: str
    status: str  # one of STAGE_STATUSES
    output: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    duration_ms: int = 0

    def __post_init__(self) -> None:
        if self.status not in STAGE_STATUSES:
            raise ValueError(f"unknown stage status: {self.status}")


@dataclass(frozen=True)
class Response:
    status: str  # one of RESPONSE_STATUSES
    trace: tuple[StageResult, ...]
    result: Any = None
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in RESPONSE_STATUSES:
            raise ValueError(f"unknown response status: {self.status}")


StageFn = Callable[[Request, list[StageResult]], StageResult]


def _ok(stage: str, output: dict[str, Any]) -> StageResult:
    return StageResult(stage=stage, status="ok", output=output)


def _failed(stage: str, reason: str) -> StageResult:
    return StageResult(stage=stage, status="failed", reason=reason)


def database_names() -> list[str]:
    return list(load_yaml(PROJECT_ROOT / "config.yaml")["databases"])


def stage_submit(request: Request, trace: list[StageResult]) -> StageResult:
    return _ok(
        "submit",
        {
            "database": request.database,
            "question": request.question,
            "credentials_supplied": request.credentials is not None,
        },
    )


def stage_resolve(request: Request, trace: list[StageResult]) -> StageResult:
    try:
        resolved = resolve(request.database)
    except CatalogError as error:
        return _failed("resolve", str(error))
    return _ok(
        "resolve",
        {
            "database": resolved.database,
            "registry_entry": resolved.registry_entry,
            "physical_file": resolved.physical_file,
            "semantic_file": resolved.semantic_file,
            "db_file": resolved.db_file,
            "tables": resolved.table_count,
            "columns": resolved.column_count,
        },
    )


def stage_prompt(request: Request, trace: list[StageResult]) -> StageResult:
    try:
        resolved = resolve(request.database)
    except CatalogError as error:
        return _failed("prompt", str(error))
    retrieval = _retrieval_config()
    limit = int(retrieval["max_prompt_chars"])
    try:
        prompt = build_prompt(request.question, resolved, limit, int(retrieval["value_list_threshold"]))
    except PromptError as error:
        return StageResult(stage="prompt", status="failed", reason=error.reason, output={"chars": error.chars, "limit": error.limit})
    return _ok("prompt", {"prompt": prompt, "chars": len(prompt), "limit": limit})


def stage_llm(request: Request, trace: list[StageResult]) -> StageResult:
    prompt = next(r.output["prompt"] for r in trace if r.stage == "prompt")
    settings = load_yaml(PROJECT_ROOT / "config.yaml")["llm"]
    creds = request.credentials
    try:
        raw_reply = complete(
            prompt,
            model_id=settings["model_id"],
            region=settings["region"],
            temperature=float(settings["temperature"]),
            max_output_tokens=int(settings["max_output_tokens"]),
            access_key_id=creds.access_key_id if creds else None,
            secret_access_key=creds.secret_access_key if creds else None,
            session_token=creds.session_token if creds else None,
            on_chunk=request.on_chunk,
        )
    except LlmError as error:
        return _failed("llm", str(error))
    return _ok("llm", {"raw_reply": raw_reply, "model_id": settings["model_id"], "region": settings["region"]})


def stage_plan(request: Request, trace: list[StageResult]) -> StageResult:
    raw_reply = next(r.output["raw_reply"] for r in trace if r.stage == "llm")
    try:
        plan = parse_reply(raw_reply)
    except CannotAnswer as answer:
        return StageResult(
            stage="plan", status="failed", reason=answer.reason, output={"cannot_answer": True, "reason": answer.reason}
        )
    except PlanError as error:
        return StageResult(stage="plan", status="failed", reason="; ".join(error.reasons), output={"reasons": list(error.reasons)})
    return _ok("plan", {"plan": plan})


def stage_match(request: Request, trace: list[StageResult]) -> StageResult:
    plan = next(r.output["plan"] for r in trace if r.stage == "plan")
    try:
        resolved = resolve(request.database)
    except CatalogError as error:
        return _failed("match", str(error))
    try:
        matched = match_plan(plan, resolved, lookup=make_lookup(resolved))
    except MatchError as error:
        return StageResult(stage="match", status="failed", reason="; ".join(error.reasons), output={"reasons": list(error.reasons)})
    return _ok("match", {"matched_plan": matched})


def stage_build_sql(request: Request, trace: list[StageResult]) -> StageResult:
    matched = next(r.output["matched_plan"] for r in trace if r.stage == "match")
    try:
        resolved = resolve(request.database)
        built = build_sql(matched, resolved.physical["schema"], Path(resolved.db_file).stem, _default_row_limit())
    except (CatalogError, BuildError) as error:
        return _failed("build_sql", str(error))
    return _ok("build_sql", built)


def _retrieval_config() -> dict[str, Any]:
    return load_yaml(PROJECT_ROOT / "config.yaml")["retrieval"]


def _default_row_limit() -> int:
    return int(_retrieval_config()["default_row_limit"])


def stage_validate(request: Request, trace: list[StageResult]) -> StageResult:
    built = next(r.output for r in trace if r.stage == "build_sql")
    try:
        resolved = resolve(request.database)
    except CatalogError as error:
        return _failed("validate", str(error))
    verdict = validate(built, resolved, make_lookup(resolved), _default_row_limit())
    if not verdict["valid"]:
        return StageResult(stage="validate", status="failed", reason="; ".join(verdict["reasons"]), output=verdict)
    return _ok("validate", verdict)


def stage_execute(request: Request, trace: list[StageResult]) -> StageResult:
    built = next(r.output for r in trace if r.stage == "build_sql")
    aggregate = next(r.output["matched_plan"] for r in trace if r.stage == "match").get("aggregation") is not None
    try:
        resolved = resolve(request.database)
        result = execute(built, resolved.db_file, _default_row_limit(), aggregate)
    except CatalogError as error:
        return _failed("execute", str(error))
    except ExecuteError as error:
        return _failed("execute", error.message)
    return _ok("execute", result)


def stage_respond(request: Request, trace: list[StageResult]) -> StageResult:
    return _ok("respond", {"status": "ok"})


STAGES: tuple[tuple[str, StageFn], ...] = (
    ("submit", stage_submit),
    ("resolve", stage_resolve),
    ("prompt", stage_prompt),
    ("llm", stage_llm),
    ("plan", stage_plan),
    ("match", stage_match),
    ("build_sql", stage_build_sql),
    ("validate", stage_validate),
    ("execute", stage_execute),
    ("respond", stage_respond),
)


def run(request: Request, on_stage_done: Callable[[StageResult], None]) -> list[StageResult]:
    """Run the stages in order. The first failure marks every later stage as skipped. Each stage is logged and reported once, in order."""
    _attach_log_file()
    trace: list[StageResult] = []
    stopped = False
    for name, stage_fn in STAGES:
        if stopped:
            result = StageResult(stage=name, status="skipped")
        else:
            started = time.perf_counter()
            result = _run_stage(name, stage_fn, request, trace)
            result = replace(result, duration_ms=round((time.perf_counter() - started) * 1000))
            stopped = result.status == "failed"
        trace.append(result)
        LOGGER.info(_log_line(result))
        on_stage_done(result)
    return trace


def _run_stage(name: str, stage_fn: StageFn, request: Request, trace: list[StageResult]) -> StageResult:
    try:
        return stage_fn(request, trace)
    except Exception as error:
        LOGGER.error("stage %s raised %s", name, type(error).__name__, exc_info=True)
        return _failed(name, f"Unexpected error in {name} ({type(error).__name__}).")


def _log_line(result: StageResult) -> str:
    """One JSON line per stage. Prompt text and result rows are left out: the log keeps the prompt size and the row count."""
    output = dict(result.output)
    if result.stage == "prompt":
        output.pop("prompt", None)
    if result.stage == "execute":
        output.pop("rows", None)
    return json.dumps(
        {"stage": result.stage, "status": result.status, "duration_ms": result.duration_ms, "reason": result.reason, "output": output},
        default=str,
    )


def _attach_log_file() -> None:
    path = PROJECT_ROOT / load_yaml(PROJECT_ROOT / "config.yaml")["logging"]["file"]
    if any(getattr(handler, "baseFilename", None) == str(path) for handler in LOGGER.handlers):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)


REJECTED_STAGES = ("plan", "match", "validate")


def final_status(trace: list[StageResult]) -> str:
    failed = next((result for result in trace if result.status == "failed"), None)
    if failed is None:
        return "ok"
    if failed.stage == "plan" and failed.output.get("cannot_answer"):
        return "cannot_answer"
    if failed.stage in REJECTED_STAGES:
        return "rejected"
    return "error"


def make_response(trace: list[StageResult], result: Any = None, reasons: tuple[str, ...] = ()) -> Response:
    return Response(status=final_status(trace), trace=tuple(trace), result=result, reasons=reasons)

"""Benchmark runner for the 30 evaluation questions (phase F6).

Each question is sent through run_pipeline. Answerable results are compared with
their independent oracle (src.evaluation.oracle). One JSON report is written per run.

Modes:
- mocked: an offline client declines every question without calling a model. It checks
  runner and report wiring only and measures no answer quality.
- live: the configured Bedrock planner and SQL generator. Needs AWS credentials,
  for example AWS_PROFILE=multidb.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
import yaml

from src.evaluation.oracle import ORACLES, OracleError, compare_frames, run_oracle
from src.query.config import Registry, load_registry, select_database
from src.query.llm import make_client
from src.query.observability import LOGGED_OUTPUT_KEYS, Response, StageResult
from src.query.pipeline import run_pipeline

ROOT = Path(__file__).resolve().parents[2]
QUESTIONS_PATH = ROOT / "specs" / "retrival" / "evaluation_question.md"
MANIFEST_PATH = ROOT / "tests" / "benchmark" / "manifest.yaml"
REPORT_DIR = ROOT / "eval_results"
MODES = ("mocked", "live")
PREVIEW_ROWS = 20
PROBLEM_LIMIT = 5

HEADER_RE = re.compile(r"^### ([A-Z]{2}\d{2}) — (.+)$", re.MULTILINE)
BODY_END_RE = re.compile(r"^(?:\*\*Tags:\*\*|\*\*Caveat:\*\*|## )", re.MULTILINE)

# Response statuses that count as the correct behaviour for each manifest status.
EXPECTED_STATUSES: dict[str, frozenset[str]] = {
    "answerable": frozenset({"ok"}),
    "clarification": frozenset({"needs_clarification"}),
    "needs_config": frozenset({"needs_clarification", "cannot_answer"}),
    "profile_first": frozenset({"needs_clarification", "cannot_answer"}),
    "gap": frozenset({"cannot_answer"}),
}
FALSE_ANSWER_OUTCOMES = frozenset({"unexpected_answer", "unexpected_status", "harness_error"})


@dataclass(frozen=True)
class Question:
    id: str
    title: str
    text: str
    database: str
    manifest_status: str


class OfflineClient:
    """Stand-in LLM for mocked runs. Declines every question without calling a model."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str, on_chunk: Callable[[str], None] | None = None) -> str:
        self.calls += 1
        return json.dumps(
            {
                "status": "cannot_answer",
                "analysis": "",
                "clarification": {"message": "Offline mock run: no model was called.", "missing": []},
            }
        )


def parse_questions(markdown: str) -> dict[str, tuple[str, str]]:
    """Map question ID to (title, prompt text) from the evaluation document."""
    headers = list(HEADER_RE.finditer(markdown))
    parsed: dict[str, tuple[str, str]] = {}
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(markdown)
        body = BODY_END_RE.split(markdown[header.end() : end], maxsplit=1)[0]
        text = " ".join(token for token in body.split() if token != "---")
        parsed[header.group(1)] = (header.group(2).strip(), text)
    return parsed


def load_questions(
    questions_path: Path = QUESTIONS_PATH, manifest_path: Path = MANIFEST_PATH
) -> list[Question]:
    parsed = parse_questions(questions_path.read_text(encoding="utf-8"))
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))["questions"]
    by_id = {entry["id"]: entry for entry in manifest}
    if set(parsed) != set(by_id):
        raise ValueError(
            f"question IDs differ: only in document {sorted(set(parsed) - set(by_id))}, "
            f"only in manifest {sorted(set(by_id) - set(parsed))}"
        )
    return [
        Question(
            id=qid,
            title=title,
            text=text,
            database=by_id[qid]["database"],
            manifest_status=by_id[qid]["status"],
        )
        for qid, (title, text) in parsed.items()
    ]


def classify_outcome(manifest_status: str, response_status: str) -> str:
    """Compare the pipeline status with the manifest's expectation for the question."""
    if response_status == "harness_error":
        return "harness_error"
    if response_status in EXPECTED_STATUSES[manifest_status]:
        return "as_expected"
    if manifest_status == "answerable":
        return "not_answered"
    if response_status == "ok":
        return "unexpected_answer"
    return "unexpected_status"


def _plain(value: Any) -> Any:
    return float(value) if isinstance(value, Decimal) else value


def _frame(result: dict[str, Any]) -> pd.DataFrame:
    rows = [tuple(_plain(v) for v in row) for row in result.get("rows", [])]
    return pd.DataFrame(rows, columns=list(result.get("columns", [])))


def judge(question_id: str, response: Response, database_file: Path) -> dict[str, Any]:
    """Compare an ok response with each oracle part; the closest part is reported on failure."""
    if question_id not in ORACLES:
        return {"verdict": "no_oracle"}
    if response.status != "ok":
        return {"verdict": "not_run"}
    spec = ORACLES[question_id]
    try:
        expected = run_oracle(spec, database_file)
    except (OracleError, duckdb.Error) as exc:
        return {"verdict": "oracle_error", "problems": [str(exc)]}
    actual = _frame(response.result)
    closest: tuple[str, list[str]] | None = None
    for part in spec.parts:
        problems = compare_frames(expected[part.name], actual, key_columns=part.key_columns)
        if not problems:
            return {"verdict": "pass", "matched_part": part.name, "problems": []}
        if closest is None or len(problems) < len(closest[1]):
            closest = (part.name, problems)
    assert closest is not None
    part_name, problems = closest
    verdict = "column_mismatch" if problems[0].startswith("columns differ") else "value_mismatch"
    return {"verdict": verdict, "closest_part": part_name, "problems": problems[:PROBLEM_LIMIT]}


def _stage_record(stage: StageResult) -> dict[str, Any]:
    return {
        "stage": stage.stage,
        "status": stage.status,
        "category": stage.category,
        "reason": stage.reason,
        "duration_ms": round(stage.duration_ms, 1),
        "output": {key: stage.output[key] for key in LOGGED_OUTPUT_KEYS if key in stage.output},
    }


def run_question(
    question: Question, registry: Registry, planner: Any, sql_generator: Any
) -> dict[str, Any]:
    base = {
        "id": question.id,
        "title": question.title,
        "database": question.database,
        "manifest_status": question.manifest_status,
    }
    started = time.perf_counter()
    try:
        response = run_pipeline(
            question.text,
            question.database,
            registry=registry,
            planner=planner,
            sql_generator=sql_generator,
            request_id=f"bench-{question.id}",
        )
    except Exception as exc:  # noqa: BLE001 - pipeline raises only for bugs; record and keep going
        return {
            **base,
            "response_status": "harness_error",
            "outcome": "harness_error",
            "harness_error": f"{type(exc).__name__}: {exc}",
            "oracle": {"verdict": "not_run"},
        }
    wall_ms = (time.perf_counter() - started) * 1000
    database_file = select_database(registry, question.database).database_file
    result = response.result
    preview = None
    if result:
        preview = {
            "columns": list(result["columns"]),
            "rows": [list(row) for row in result["rows"][:PREVIEW_ROWS]],
        }
    return {
        **base,
        "response_status": response.status,
        "category": response.category,
        "reasons": list(response.reasons),
        "outcome": classify_outcome(question.manifest_status, response.status),
        "oracle": judge(question.id, response, database_file),
        "sql": response.sql,
        "params": list(response.params),
        "row_count": result.get("row_count"),
        "truncated": result.get("truncated"),
        "wall_ms": round(wall_ms, 1),
        "stage_ms": round(sum(s.duration_ms for s in response.trace), 1),
        "stages": [_stage_record(s) for s in response.trace],
        "result_preview": preview,
    }


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "questions": len(records),
        "response_status": dict(Counter(r["response_status"] for r in records)),
        "outcome": dict(Counter(r["outcome"] for r in records)),
        "oracle": dict(Counter(r["oracle"]["verdict"] for r in records)),
        "verified": [r["id"] for r in records if r["oracle"]["verdict"] == "pass"],
        "not_answered": [r["id"] for r in records if r["outcome"] == "not_answered"],
        "false_or_unexpected": [r["id"] for r in records if r["outcome"] in FALSE_ANSWER_OUTCOMES],
    }


def run_benchmark(
    mode: str,
    questions: list[Question],
    registry: Registry,
    planner: Any,
    sql_generator: Any,
    models: dict[str, str],
) -> dict[str, Any]:
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    records = [run_question(q, registry, planner, sql_generator) for q in questions]
    return {
        "run": {
            "mode": mode,
            "started_at": started_at,
            "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "models": models,
            "questions_file": QUESTIONS_PATH.relative_to(ROOT).as_posix(),
            "manifest_file": MANIFEST_PATH.relative_to(ROOT).as_posix(),
        },
        "summary": summarize(records),
        "questions": records,
    }


def write_report(report: dict[str, Any], report_dir: Path) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromisoformat(report["run"]["started_at"]).strftime("%Y%m%dT%H%M%SZ")
    path = report_dir / f"benchmark-{report['run']['mode']}-{stamp}.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return path


def _print_summary(report: dict[str, Any], path: Path) -> None:
    summary = report["summary"]
    print(f"mode: {report['run']['mode']}  questions: {summary['questions']}")
    print(f"response status: {summary['response_status']}")
    print(f"outcome: {summary['outcome']}")
    print(f"oracle: {summary['oracle']}")
    print(f"verified: {summary['verified']}")
    print(f"not answered: {summary['not_answered']}")
    print(f"false or unexpected: {summary['false_or_unexpected']}")
    print(f"report: {path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the 30-question benchmark.")
    parser.add_argument("--mode", choices=MODES, required=True)
    parser.add_argument("--ids", nargs="+", metavar="ID", help="Run only these question IDs.")
    parser.add_argument("--report-dir", type=Path, default=REPORT_DIR)
    args = parser.parse_args(argv)

    registry = load_registry()
    questions = load_questions()
    if args.ids:
        known = {q.id for q in questions}
        unknown = sorted(set(args.ids) - known)
        if unknown:
            parser.error(f"unknown question IDs: {unknown}")
        questions = [q for q in questions if q.id in set(args.ids)]

    if args.mode == "mocked":
        planner: Any = OfflineClient()
        sql_generator: Any = OfflineClient()
        models = {"planner": "offline", "sql_generator": "offline"}
    else:
        planner = make_client(registry.planner_llm)
        sql_generator = make_client(registry.sql_generator_llm)
        models = {
            "planner": f"{registry.planner_llm.model_id} ({registry.planner_llm.region})",
            "sql_generator": f"{registry.sql_generator_llm.model_id} ({registry.sql_generator_llm.region})",
        }

    report = run_benchmark(args.mode, questions, registry, planner, sql_generator, models)
    path = write_report(report, args.report_dir)
    _print_summary(report, path)
    return 1 if report["summary"]["outcome"].get("harness_error") else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Streamlit AppTest checks for the Ask tab.

The pipeline and the LLM clients are faked, so these tests need no network and no data files.
"""

import dataclasses
import unittest
from pathlib import Path
from unittest import mock

from streamlit.testing.v1 import AppTest

from src.query import pipeline
from src.query.observability import Response, StageResult

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "src" / "ui" / "app.py"
SQL = "SELECT region, SUM(sales) AS sales FROM orders GROUP BY 1"


class FakeLlm:
    def __init__(self) -> None:
        self.on_chunk_seen: list[bool] = []

    def complete(self, prompt: str, on_chunk=None) -> str:
        self.on_chunk_seen.append(on_chunk is not None)
        if on_chunk is not None:
            on_chunk("SELECT ")
        return SQL


def build_trace(failure: StageResult | None = None) -> tuple[StageResult, ...]:
    trace: list[StageResult] = []
    stopped = False
    for name in pipeline.STAGES:
        if stopped:
            trace.append(StageResult(stage=name, status="skipped", reason="earlier stage failed"))
        elif failure is not None and failure.stage == name:
            trace.append(failure)
            stopped = True
        else:
            trace.append(StageResult(stage=name, status="ok", output={"characters": 9}, duration_ms=2.0))
    return tuple(trace)


class FakePipeline:
    """Stands in for run_pipeline: calls the LLM wrappers, emits the trace, returns a fixed response."""

    def __init__(self, response: Response, failure: StageResult | None = None) -> None:
        self.response = response
        self.failure = failure
        self.calls: list[tuple[str, str]] = []

    def __call__(self, question, database_key, *, registry=None, planner=None, sql_generator=None,
                 request_id=None, on_stage=None):
        self.calls.append((question, database_key))
        if planner is not None:
            planner.complete("planner prompt")
        if sql_generator is not None:
            sql_generator.complete("sql prompt")
        if on_stage is not None:
            for result in self.response.trace:
                on_stage(result)
        return self.response


def ok_response() -> Response:
    return Response(
        status="ok",
        trace=build_trace(),
        result={
            "columns": ["region", "sales"],
            "rows": [("West", 10.0), ("East", 7.5)],
            "row_count": 2,
            "truncated": False,
        },
        sql=SQL,
        params=(),
    )


class AskTabTests(unittest.TestCase):
    def setUp(self):
        self.llm = FakeLlm()
        for target in ("src.query.llm.make_client", "src.query.sql_generator.make_client"):
            patcher = mock.patch(target, return_value=self.llm)
            patcher.start()
            self.addCleanup(patcher.stop)

    def patch_pipeline(self, fake: FakePipeline) -> None:
        patcher = mock.patch("src.query.pipeline.run_pipeline", new=fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def ask(self, question: str) -> AppTest:
        at = AppTest.from_file(str(APP), default_timeout=60).run()
        if question:
            at.text_area(key="question").input(question).run()
        ask_button = next(b for b in at.button if b.label == "Ask")
        return ask_button.click().run()

    def test_empty_question_warns_and_does_not_run(self):
        fake = FakePipeline(ok_response())
        self.patch_pipeline(fake)
        at = self.ask("")
        self.assertEqual(at.exception, [])
        self.assertIn("Enter a question first.", [w.value for w in at.warning])
        self.assertEqual(fake.calls, [])

    def test_success_renders_rows_stage_panels_and_sql(self):
        fake = FakePipeline(ok_response())
        self.patch_pipeline(fake)
        at = self.ask("total sales by region")
        self.assertEqual(at.exception, [])
        self.assertEqual(fake.calls, [("total sales by region", at.selectbox(key="database").value)])
        self.assertIn("2 row(s) returned", " ".join(s.value for s in at.success))
        labels = [e.label for e in at.expander]
        self.assertIn("Final SQL", labels)
        final_sql = next(e for e in at.expander if e.label == "Final SQL")
        self.assertEqual([c.value for c in final_sql.code], [SQL])
        stage_labels = [label for label in labels if label != "Final SQL"]
        self.assertEqual(len(stage_labels), len(pipeline.STAGES))
        self.assertTrue(stage_labels[0].startswith("1. submit"))
        self.assertIn(SQL, [c.value for c in at.code])
        self.assertGreaterEqual(len(at.dataframe), 2)

    def test_stage_panels_live_in_the_steps_tab(self):
        fake = FakePipeline(ok_response())
        self.patch_pipeline(fake)
        at = self.ask("total sales by region")
        steps_tab = at.tabs[1]
        self.assertEqual(len(steps_tab.expander), len(pipeline.STAGES))

    def test_llm_wrappers_pass_chunk_callbacks_to_the_client(self):
        fake = FakePipeline(ok_response())
        self.patch_pipeline(fake)
        self.ask("total sales by region")
        self.assertIn(True, self.llm.on_chunk_seen)

    def test_stage_panels_show_detail_for_each_stage(self):
        trace = build_trace()
        trace = tuple(
            dataclasses.replace(
                r,
                detail={"planner_reply": "ANALYSIS-TEXT", "sql": SQL, "preview_rows": [["West", 10.0]],
                        "columns": ["region", "sales"], "preview_row_limit": 50}
            ) if r.stage == "sql_generator_llm" else r
            for r in trace
        )
        response = Response(status="ok", trace=trace, result=ok_response().result, sql=SQL, params=())
        self.patch_pipeline(FakePipeline(response))
        at = self.ask("total sales by region")
        self.assertEqual(at.exception, [])
        self.assertIn("ANALYSIS-TEXT", [c.value for c in at.code] + [c.value for c in at.caption])
        self.assertIn(SQL, [c.value for c in at.code])

    def test_failure_shows_reason_without_traceback(self):
        failure = StageResult(
            stage="planner_llm",
            status="failed",
            reason="LLM call failed: ClientError",
            duration_ms=3.0,
            category="llm_error",
        )
        response = Response(
            status="error",
            trace=build_trace(failure),
            category="llm_error",
            reasons=("LLM call failed: ClientError",),
        )
        self.patch_pipeline(FakePipeline(response, failure))
        at = self.ask("total sales by region")
        self.assertEqual(at.exception, [])
        self.assertIn("Error: LLM call failed: ClientError", [e.value for e in at.error])
        planner_number = pipeline.STAGES.index("planner_llm") + 1
        planner_panel = next(e for e in at.expander if e.label.startswith(f"{planner_number}. planner_llm"))
        self.assertIn("failed", planner_panel.label)

    def test_clarification_offers_metric_confirmation_for_missing_terms(self):
        failure = StageResult(
            stage="parse_analysis",
            status="failed",
            output={"status": "needs_clarification", "missing": ["profit margin"]},
            reason="Undefined business terms: profit margin",
            duration_ms=1.0,
            category="clarification_needed",
        )
        response = Response(
            status="needs_clarification",
            trace=build_trace(failure),
            category="clarification_needed",
            reasons=("Undefined business terms: profit margin",),
        )
        self.patch_pipeline(FakePipeline(response, failure))
        render = mock.MagicMock()
        with mock.patch("src.query.catalog.load_catalog", return_value=mock.MagicMock()), \
                mock.patch("src.query.metrics.bedrock_proposer", return_value=mock.MagicMock()), \
                mock.patch("src.ui.metric_confirmation.render_metric_confirmation", new=render):
            at = self.ask("what is the profit margin by region?")
        self.assertEqual(at.exception, [])
        self.assertIn("no approved metric definition", " ".join(i.value for i in at.info))
        self.assertEqual(render.call_count, 1)
        self.assertEqual(render.call_args.args[3], "profit margin")
        self.assertEqual(render.call_args.args[4], "what is the profit margin by region?")

    def assert_outcome_label(self, stage: str, category: str, status: str, label: str) -> None:
        failure = StageResult(stage=stage, status="failed", reason=f"{label} reason", duration_ms=1.0,
                              category=category)
        response = Response(
            status=status,
            trace=build_trace(failure),
            category=category,
            reasons=(f"{label} reason",),
        )
        self.patch_pipeline(FakePipeline(response, failure))
        at = self.ask("which region had the highest margin?")
        self.assertEqual(at.exception, [])
        self.assertIn(f"{label}: {label} reason", [e.value for e in at.error])
        self.assertEqual(at.success, [])

    def test_cannot_answer_is_distinct_from_errors(self):
        self.assert_outcome_label("parse_analysis", "unsupported", "cannot_answer", "Cannot answer")

    def test_rejected_sql_is_labelled_rejected(self):
        self.assert_outcome_label("validate_sql_ast", "invalid_sql", "rejected", "Rejected")

    def test_steps_tab_is_empty_before_a_run(self):
        at = AppTest.from_file(str(APP), default_timeout=60).run()
        self.assertEqual(at.exception, [])
        self.assertIn("Run a question on the Ask tab", " ".join(i.value for i in at.info))

    def test_run_sql_tab_is_a_placeholder(self):
        at = AppTest.from_file(str(APP), default_timeout=60).run()
        self.assertIn("Manual SQL is not part of this build yet", " ".join(i.value for i in at.info))


if __name__ == "__main__":
    unittest.main()

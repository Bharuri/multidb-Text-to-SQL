import json
import unittest
from pathlib import Path
from unittest import mock

from src.query import pipeline
from src.query.catalog import PROJECT_ROOT, load_yaml, resolve
from src.query.llm import LlmError, SqlReply, SqlReplyError
from src.query.pipeline import STAGE_NAMES, Credentials, Request, StageResult, final_status, run

ACCESS = "AKIAFAKE123456"
SECRET = "secret-fake-value-xyz"
PHONES_COUNT = {
    "table": "Orders",
    "select": [],
    "dimensions": [],
    "metrics": [{"function": "COUNT", "column": "*", "alias": "n"}],
    "filters": [{"column": "Sub_Category", "operator": "=", "value": "Phones"}],
    "group_by": [],
    "having": [],
    "order_by": [],
    "calculations": [],
    "limit": None,
}
_CATALOG = resolve("sample_superstore")
PHONES_SQL = (
    f'SELECT COUNT(*) AS n FROM "{Path(_CATALOG.db_file).stem}"."{_CATALOG.physical["schema"]}"."Orders" WHERE LOWER("Sub_Category") = LOWER(?)'
)
PHONES_PARAMS = ["Phones"]


def fake_complete(chunks: list[str], reply: str):
    def call(prompt: str, **kwargs) -> str:
        for chunk in chunks:
            kwargs["on_chunk"](chunk)
        return reply

    return call


def fake_generate_sql(chunks: list[str], sql: str = PHONES_SQL, params: list = PHONES_PARAMS):
    def call(prompt: str, **kwargs) -> SqlReply:
        for chunk in chunks:
            kwargs["on_chunk"](chunk)
        return SqlReply(raw=json.dumps({"sql": sql, "params": params}), sql=sql, params=params)

    return call


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch("src.query.pipeline._attach_log_file")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_each_stage_is_reported_once_in_order(self) -> None:
        reported: list[str] = []
        with mock.patch("src.query.pipeline.complete") as llm_call:
            trace = run(Request(database="no_such_db", question="q"), on_stage_done=lambda r: reported.append(r.stage))
        self.assertEqual(reported, list(STAGE_NAMES))
        self.assertEqual([r.stage for r in trace], list(STAGE_NAMES))
        llm_call.assert_not_called()

    def test_a_failure_marks_every_later_stage_skipped(self) -> None:
        with mock.patch("src.query.pipeline.complete"):
            trace = run(Request(database="no_such_db", question="q"), on_stage_done=lambda r: None)
        self.assertEqual(trace[0].status, "ok")
        self.assertEqual(trace[1].status, "failed")
        self.assertTrue(all(r.status == "skipped" for r in trace[2:]))
        self.assertEqual(final_status(trace), "error")

    def test_log_lines_match_the_trace(self) -> None:
        with self.assertLogs("multidb.query", level="INFO") as captured:
            trace = run(Request(database="no_such_db", question="q"), on_stage_done=lambda r: None)
        logged = [json.loads(record.getMessage()) for record in captured.records]
        self.assertEqual(len(logged), len(trace))
        for entry, result in zip(logged, trace):
            self.assertEqual(entry["stage"], result.stage)
            self.assertEqual(entry["status"], result.status)
            self.assertEqual(entry["reason"], result.reason)

    def test_unexpected_exception_becomes_a_failed_stage(self) -> None:
        with mock.patch("src.query.pipeline.resolve", side_effect=RuntimeError("internal detail")):
            with self.assertLogs("multidb.query", level="ERROR"):
                trace = run(Request(database="sample_superstore", question="q"), on_stage_done=lambda r: None)
        resolve_stage = next(r for r in trace if r.stage == "resolve")
        self.assertEqual(resolve_stage.status, "failed")
        self.assertEqual(resolve_stage.reason, "Unexpected error in resolve (RuntimeError).")
        self.assertNotIn("internal detail", repr(trace))
        self.assertEqual(final_status(trace), "error")


class EndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch("src.query.pipeline._attach_log_file")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_phones_question_runs_all_stages(self) -> None:
        reply = json.dumps(PHONES_COUNT)
        with mock.patch("src.query.pipeline.complete", side_effect=fake_complete([], reply)), mock.patch(
            "src.query.pipeline.generate_sql", side_effect=fake_generate_sql([])
        ):
            with self.assertLogs("multidb.query", level="INFO") as captured:
                trace = run(
                    Request(database="sample_superstore", question="How many orders are phones?"),
                    on_stage_done=lambda r: None,
                )
        self.assertEqual(final_status(trace), "ok")
        self.assertEqual([r.stage for r in trace], list(STAGE_NAMES))
        sql_stage = next(r for r in trace if r.stage == "sql_generate")
        self.assertEqual(sql_stage.output["path"], "llm")
        self.assertEqual(sql_stage.output["sql"], PHONES_SQL)
        execute_stage = next(r for r in trace if r.stage == "execute")
        self.assertEqual(execute_stage.output["row_count"], 1)
        respond_stage = next(r for r in trace if r.stage == "respond")
        self.assertEqual(respond_stage.output["sql"], PHONES_SQL)
        self.assertEqual(respond_stage.output["params"], PHONES_PARAMS)
        dump = "\n".join(record.getMessage() for record in captured.records)
        self.assertNotIn("Tables and columns", dump)
        self.assertNotIn('"rows"', dump)
        self.assertNotIn('"raw_reply"', dump)
        self.assertIn('"row_count": 1', dump)

    def test_credentials_never_appear_in_the_log_or_trace(self) -> None:
        creds = Credentials(access_key_id=ACCESS, secret_access_key=SECRET, session_token="tok-fake")
        request = Request(database="sample_superstore", question="How many orders are phones?", credentials=creds)
        with mock.patch("src.query.pipeline.complete", side_effect=fake_complete([], json.dumps(PHONES_COUNT))), mock.patch(
            "src.query.pipeline.generate_sql", side_effect=fake_generate_sql([])
        ):
            with self.assertLogs("multidb.query", level="INFO") as captured:
                trace = run(request, on_stage_done=lambda r: None)
        dump = "\n".join(record.getMessage() for record in captured.records) + repr(trace) + repr(request)
        self.assertNotIn(ACCESS, dump)
        self.assertNotIn(SECRET, dump)
        self.assertNotIn("tok-fake", dump)
        submit = next(r for r in trace if r.stage == "submit")
        self.assertTrue(submit.output["credentials_supplied"])

    def test_planner_and_sql_streams_reach_their_own_callbacks(self) -> None:
        planner_chunks: list[str] = []
        sql_chunks: list[str] = []
        request = Request(
            database="sample_superstore",
            question="How many orders are phones?",
            on_chunk=planner_chunks.append,
            on_sql_chunk=sql_chunks.append,
        )
        reply = json.dumps(PHONES_COUNT)
        with mock.patch(
            "src.query.pipeline.complete", side_effect=fake_complete(['{"table"', ': "Orders"}'], reply)
        ), mock.patch("src.query.pipeline.generate_sql", side_effect=fake_generate_sql(['{"sql"', ': "x"}'])):
            trace = run(request, on_stage_done=lambda r: None)
        self.assertEqual(planner_chunks, ['{"table"', ': "Orders"}'])
        self.assertEqual(sql_chunks, ['{"sql"', ': "x"}'])
        self.assertEqual(final_status(trace), "ok")


class SqlGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch("src.query.pipeline._attach_log_file")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run_with_generator(self, side_effect):
        with mock.patch("src.query.pipeline.complete", side_effect=fake_complete([], json.dumps(PHONES_COUNT))), mock.patch(
            "src.query.pipeline.generate_sql", side_effect=side_effect
        ):
            return run(Request(database="sample_superstore", question="How many orders are phones?"), on_stage_done=lambda r: None)

    def test_sql_generator_failure_stops_the_run(self) -> None:
        trace = self._run_with_generator(LlmError("LLM call failed (ThrottlingException)."))
        failed = next(r for r in trace if r.status == "failed")
        self.assertEqual(failed.stage, "sql_generate")
        self.assertEqual(failed.reason, "LLM call failed (ThrottlingException).")
        later = [r.status for r in trace if r.stage in ("validate", "execute", "respond")]
        self.assertEqual(later, ["skipped", "skipped", "skipped"])
        self.assertEqual(final_status(trace), "error")

    def test_malformed_sql_reply_fails_with_the_raw_text_in_the_trace(self) -> None:
        trace = self._run_with_generator(SqlReplyError(["reply is not one JSON object"], "not json"))
        stage = next(r for r in trace if r.stage == "sql_generate")
        self.assertEqual(stage.status, "failed")
        self.assertEqual(stage.reason, "reply is not one JSON object")
        self.assertEqual(stage.output["raw_reply"], "not json")

    def test_builder_path_skips_the_llm_when_enabled(self) -> None:
        trace = [
            StageResult(stage="match", status="ok", output={"matched_plan": {"table": "Orders"}}),
            StageResult(stage="sql_prompt", status="ok", output={"prompt": "p"}),
        ]
        built = {"sql": "SELECT 1", "params": []}
        with mock.patch.object(pipeline, "_capabilities", return_value={"simple_sql_builder": True}), mock.patch.object(
            pipeline, "build_sql", return_value=built
        ), mock.patch.object(pipeline, "generate_sql") as llm_call:
            result = pipeline.stage_sql_generate(Request(database="sample_superstore", question="q"), trace)
        llm_call.assert_not_called()
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.output, {"path": "builder", "sql": "SELECT 1", "params": []})

    def test_plans_the_builder_cannot_handle_go_to_the_llm(self) -> None:
        trace = [
            StageResult(stage="match", status="ok", output={"matched_plan": {"table": "Orders"}}),
            StageResult(stage="sql_prompt", status="ok", output={"prompt": "p"}),
        ]
        with mock.patch.object(pipeline, "_capabilities", return_value={"simple_sql_builder": True}), mock.patch.object(
            pipeline, "build_sql", return_value=None
        ), mock.patch.object(pipeline, "generate_sql", side_effect=fake_generate_sql([])) as llm_call:
            result = pipeline.stage_sql_generate(Request(database="sample_superstore", question="q"), trace)
        llm_call.assert_called_once()
        self.assertEqual(result.output["path"], "llm")
        self.assertEqual(result.output["sql"], PHONES_SQL)

    def test_sql_prompt_over_the_limit_fails_the_stage(self) -> None:
        retrieval = dict(load_yaml(PROJECT_ROOT / "config.yaml")["retrieval"], max_prompt_chars=10)
        trace = [StageResult(stage="match", status="ok", output={"matched_plan": PHONES_COUNT})]
        with mock.patch.object(pipeline, "_retrieval_config", return_value=retrieval):
            result = pipeline.stage_sql_prompt(Request(database="sample_superstore", question="q"), trace)
        self.assertEqual(result.stage, "sql_prompt")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.output["limit"], 10)


class BusinessStageTests(unittest.TestCase):
    def test_prompt_stage_includes_configured_business_terms(self) -> None:
        request = Request(database="sample_superstore", question="total revenue")
        result = pipeline.stage_prompt(request, [])
        self.assertEqual(result.status, "ok")
        self.assertIn("revenue = Orders.Sales", result.output["prompt"])

    def test_broken_business_file_fails_the_prompt_stage(self) -> None:
        with mock.patch.object(pipeline, "load_business", side_effect=pipeline.BusinessError(["bad file"])):
            result = pipeline.stage_prompt(Request(database="sample_superstore", question="q"), [])
        self.assertEqual(result.stage, "prompt")
        self.assertEqual(result.status, "failed")
        self.assertIn("bad file", result.reason)


class StageHelpersTests(unittest.TestCase):
    def test_database_names_come_from_config(self) -> None:
        expected = list(load_yaml(PROJECT_ROOT / "config.yaml")["databases"])
        self.assertEqual(pipeline.database_names(), expected)
        self.assertIn("sample_superstore", pipeline.database_names())


if __name__ == "__main__":
    unittest.main()

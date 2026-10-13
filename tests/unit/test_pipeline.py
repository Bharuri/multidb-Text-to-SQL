import json
import unittest
from unittest import mock

from src.query import pipeline
from src.query.catalog import PROJECT_ROOT, load_yaml
from src.query.pipeline import STAGE_NAMES, Credentials, Request, final_status, run

ACCESS = "AKIAFAKE123456"
SECRET = "secret-fake-value-xyz"
PHONES_COUNT = {
    "table": "Orders",
    "select": [],
    "aggregation": {"function": "COUNT", "column": "*"},
    "filters": [{"column": "Sub_Category", "operator": "=", "value": "Phones"}],
    "limit": None,
}


def fake_complete(chunks: list[str], reply: str):
    def call(prompt: str, **kwargs) -> str:
        for chunk in chunks:
            kwargs["on_chunk"](chunk)
        return reply

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
        with mock.patch("src.query.pipeline.complete", side_effect=fake_complete([], reply)):
            with self.assertLogs("multidb.query", level="INFO") as captured:
                trace = run(
                    Request(database="sample_superstore", question="How many orders are phones?"),
                    on_stage_done=lambda r: None,
                )
        self.assertEqual(final_status(trace), "ok")
        self.assertEqual([r.stage for r in trace], list(STAGE_NAMES))
        execute_stage = next(r for r in trace if r.stage == "execute")
        self.assertEqual(execute_stage.output["row_count"], 1)
        dump = "\n".join(record.getMessage() for record in captured.records)
        self.assertNotIn("Tables and columns", dump)
        self.assertNotIn('"rows"', dump)
        self.assertIn('"row_count": 1', dump)

    def test_credentials_never_appear_in_the_log_or_trace(self) -> None:
        creds = Credentials(access_key_id=ACCESS, secret_access_key=SECRET, session_token="tok-fake")
        request = Request(database="sample_superstore", question="How many orders are phones?", credentials=creds)
        with mock.patch("src.query.pipeline.complete", side_effect=fake_complete([], json.dumps(PHONES_COUNT))):
            with self.assertLogs("multidb.query", level="INFO") as captured:
                trace = run(request, on_stage_done=lambda r: None)
        dump = "\n".join(record.getMessage() for record in captured.records) + repr(trace) + repr(request)
        self.assertNotIn(ACCESS, dump)
        self.assertNotIn(SECRET, dump)
        self.assertNotIn("tok-fake", dump)
        submit = next(r for r in trace if r.stage == "submit")
        self.assertTrue(submit.output["credentials_supplied"])

    def test_streamed_chunks_reach_the_callback_before_the_llm_stage_finishes(self) -> None:
        chunks: list[str] = []
        request = Request(
            database="sample_superstore",
            question="How many orders are phones?",
            on_chunk=chunks.append,
        )
        reply = json.dumps(PHONES_COUNT)
        with mock.patch("src.query.pipeline.complete", side_effect=fake_complete(["{\"table\"", ": \"Orders\"}"], reply)):
            trace = run(request, on_stage_done=lambda r: None)
        self.assertEqual(chunks, ['{"table"', ': "Orders"}'])
        self.assertEqual(final_status(trace), "ok")


class StageHelpersTests(unittest.TestCase):
    def test_database_names_come_from_config(self) -> None:
        expected = list(load_yaml(PROJECT_ROOT / "config.yaml")["databases"])
        self.assertEqual(pipeline.database_names(), expected)
        self.assertIn("sample_superstore", pipeline.database_names())


if __name__ == "__main__":
    unittest.main()

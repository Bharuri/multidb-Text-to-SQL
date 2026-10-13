import json
import unittest
from unittest import mock

from src.query import pipeline
from src.query.llm import LlmError, SqlReply
from src.query.pipeline import STAGE_NAMES, STAGES, Credentials, Request, StageResult, final_status, run

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
PHONES_PARAMS = ["Phones"]
LOGGER_NAME = "multidb.query"


def _planner(prompt: str, **kwargs) -> str:
    return json.dumps(PHONES_COUNT)


def _generator(prompt: str, **kwargs) -> SqlReply:
    sql = (
        'SELECT COUNT(*) AS n FROM "sample_superstore"."sample_superstore"."Orders" '
        'WHERE LOWER("Sub_Category") = LOWER(?)'
    )
    return SqlReply(raw=json.dumps({"sql": sql, "params": PHONES_PARAMS}), sql=sql, params=PHONES_PARAMS)


def _forced_failure(stage_name: str):
    def stage(request: Request, trace: list[StageResult]) -> StageResult:
        return StageResult(stage=stage_name, status="failed", reason=f"forced failure in {stage_name}")

    return stage


class _LoggedRunTestCase(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch("src.query.pipeline._attach_log_file")
        patcher.start()
        self.addCleanup(patcher.stop)
        llm = mock.patch("src.query.pipeline.complete", side_effect=_planner)
        llm.start()
        self.addCleanup(llm.stop)
        generator = mock.patch("src.query.pipeline.generate_sql", side_effect=_generator)
        generator.start()
        self.addCleanup(generator.stop)


class FailureAtEachStageTests(_LoggedRunTestCase):
    def test_failure_at_each_stage_marks_it_failed_and_skips_the_rest(self) -> None:
        for index, (name, _) in enumerate(STAGES):
            with self.subTest(stage=name):
                stages = tuple((n, _forced_failure(n) if n == name else fn) for n, fn in STAGES)
                request = Request(database="sample_superstore", question="How many orders are phones?")
                with mock.patch.object(pipeline, "STAGES", stages), self.assertLogs(LOGGER_NAME, level="INFO") as captured:
                    trace = run(request, on_stage_done=lambda r: None)

                self.assertEqual([r.stage for r in trace], list(STAGE_NAMES))
                self.assertEqual([r.status for r in trace[:index]], ["ok"] * index)
                self.assertEqual(trace[index].status, "failed")
                self.assertEqual(trace[index].reason, f"forced failure in {name}")
                self.assertEqual(
                    [r.status for r in trace[index + 1 :]], ["skipped"] * (len(STAGE_NAMES) - index - 1)
                )
                logged = [json.loads(record.getMessage())["stage"] for record in captured.records]
                self.assertEqual(logged, list(STAGE_NAMES))
                self.assertNotEqual(final_status(trace), "ok")


class SuccessTraceTests(_LoggedRunTestCase):
    def test_both_llm_calls_are_separate_visible_stages(self) -> None:
        request = Request(database="sample_superstore", question="How many orders are phones?")
        with self.assertLogs(LOGGER_NAME, level="INFO") as captured:
            trace = run(request, on_stage_done=lambda r: None)

        self.assertEqual(final_status(trace), "ok")
        by_stage = {r.stage: r for r in trace}
        self.assertEqual(by_stage["llm"].status, "ok")
        self.assertEqual(by_stage["sql_generate"].status, "ok")
        self.assertIn("raw_reply", by_stage["llm"].output)
        self.assertNotIn("sql", by_stage["llm"].output)
        self.assertIn("sql", by_stage["sql_generate"].output)

        logged = {json.loads(record.getMessage())["stage"] for record in captured.records}
        self.assertIn("llm", logged)
        self.assertIn("sql_generate", logged)


class FailureLeakageTests(_LoggedRunTestCase):
    def test_credentials_do_not_leak_when_the_planner_call_fails(self) -> None:
        creds = Credentials(access_key_id=ACCESS, secret_access_key=SECRET, session_token="tok-fake")
        request = Request(database="sample_superstore", question="How many orders are phones?", credentials=creds)
        with mock.patch("src.query.pipeline.complete", side_effect=LlmError("LLM call failed (AccessDeniedException).")):
            with self.assertLogs(LOGGER_NAME, level="INFO") as captured:
                trace = run(request, on_stage_done=lambda r: None)

        self.assertEqual(next(r for r in trace if r.stage == "llm").status, "failed")
        dump = "\n".join(record.getMessage() for record in captured.records) + repr(trace)
        self.assertNotIn(ACCESS, dump)
        self.assertNotIn(SECRET, dump)
        self.assertNotIn("tok-fake", dump)


if __name__ == "__main__":
    unittest.main()

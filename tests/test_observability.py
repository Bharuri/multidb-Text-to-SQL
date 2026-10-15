import json
import logging
import unittest

from src.query import observability as obs

STAGES = ["submit", "resolve_database", "load_catalog", "retrieve_context"]


class StageResultTests(unittest.TestCase):
    def test_ok_stage_needs_no_category(self):
        res = obs.StageResult(stage="submit", status="ok")
        self.assertIsNone(res.category)

    def test_failed_stage_requires_known_category(self):
        with self.assertRaises(ValueError):
            obs.StageResult(stage="submit", status="failed", category="made_up")
        with self.assertRaises(ValueError):
            obs.StageResult(stage="submit", status="failed")

    def test_unknown_status_is_rejected(self):
        with self.assertRaises(ValueError):
            obs.StageResult(stage="submit", status="running")

    def test_long_output_is_bounded(self):
        big = {"rows": ["x" * 100] * 500}
        data = obs.StageResult(stage="execute", status="ok", output=big).to_dict()
        self.assertIn("summary", data["output"])
        self.assertLessEqual(len(data["output"]["summary"]), obs.MAX_OUTPUT_CHARS)

    def test_long_reason_is_bounded(self):
        data = obs.StageResult(stage="x", status="failed", category="internal_error", reason="r" * 5000).to_dict()
        self.assertLessEqual(len(data["reason"]), obs.MAX_REASON_CHARS)
        self.assertTrue(data["reason"].endswith("...[truncated]"))

    def test_small_output_is_kept_as_is(self):
        data = obs.StageResult(stage="x", status="ok", output={"n": 2}).to_dict()
        self.assertEqual(data["output"], {"n": 2})


class DetailTests(unittest.TestCase):
    def test_detail_is_excluded_from_serialization_and_repr(self):
        res = obs.StageResult(stage="planner_llm", status="ok", detail={"planner_reply": "SECRET-REPLY"})
        self.assertNotIn("SECRET-REPLY", json.dumps(res.to_dict()))
        self.assertNotIn("SECRET-REPLY", repr(res))
        self.assertEqual(res, obs.StageResult(stage="planner_llm", status="ok"))

    def test_detail_is_excluded_from_logs(self):
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Capture()
        obs.LOGGER.addHandler(handler)
        obs.LOGGER.setLevel(logging.INFO)
        try:
            obs.log_stage(
                obs.StageResult(stage="sql_generator_llm", status="ok", detail={"generator_reply": "SECRET-SQL"}),
                request_id="r2",
            )
        finally:
            obs.LOGGER.removeHandler(handler)
        self.assertEqual(len(records), 1)
        self.assertNotIn("SECRET-SQL", records[0])

    def test_bounded_detail_truncates_long_text_and_stringifies_sets(self):
        detail = obs.bounded_detail({"prompt": "p" * (obs.MAX_DETAIL_CHARS + 500), "tables": {"b", "a"}})
        self.assertLessEqual(len(detail["prompt"]), obs.MAX_DETAIL_CHARS + len("...[truncated]"))
        self.assertTrue(detail["prompt"].endswith("...[truncated]"))
        self.assertEqual(detail["tables"], ["a", "b"])
        json.dumps(detail)


class ResponseTests(unittest.TestCase):
    def test_ok_response_needs_no_category(self):
        obs.Response(status="ok")

    def test_error_response_requires_category(self):
        with self.assertRaises(ValueError):
            obs.Response(status="error")
        with self.assertRaises(ValueError):
            obs.Response(status="rejected", category="bogus")

    def test_response_serializes_trace_and_reasons(self):
        trace = (obs.StageResult(stage="submit", status="ok"),)
        data = obs.Response(status="ok", trace=trace, reasons=("fine",)).to_dict()
        self.assertEqual(data["trace"][0]["stage"], "submit")
        self.assertEqual(data["reasons"], ["fine"])
        json.dumps(data)


class SkipAndOrderTests(unittest.TestCase):
    def test_stages_after_failure_are_skipped_in_order(self):
        skipped = obs.skipped_after(STAGES, "load_catalog")
        self.assertEqual([s.stage for s in skipped], ["retrieve_context"])
        self.assertTrue(all(s.status == "skipped" for s in skipped))


class LoggingTests(unittest.TestCase):
    def test_log_record_agrees_with_trace_and_excludes_output(self):
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Capture()
        obs.LOGGER.addHandler(handler)
        obs.LOGGER.setLevel(logging.INFO)
        try:
            result = obs.StageResult(
                stage="execute", status="failed", category="execution_error",
                output={"secret": "sk-live-abc123"}, reason="boom",
            )
            obs.log_stage(result, request_id="r1")
        finally:
            obs.LOGGER.removeHandler(handler)

        self.assertEqual(len(records), 1)
        payload = json.loads(records[0].split(" ", 1)[1])
        self.assertEqual(payload["stage"], "execute")
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["category"], "execution_error")
        self.assertNotIn("sk-live-abc123", records[0])
        self.assertNotIn("boom", records[0])


if __name__ == "__main__":
    unittest.main()

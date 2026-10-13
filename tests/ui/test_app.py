import unittest
from pathlib import Path
from unittest import mock

from streamlit.testing.v1 import AppTest

from src.query.pipeline import STAGE_NAMES, StageResult

APP = Path(__file__).resolve().parents[2] / "src" / "ui" / "app.py"


OUTPUTS = {
    "sql_generate": {"sql": "SELECT 1", "params": [], "path": "llm"},
    "execute": {"columns": ["n"], "rows": [[1]], "truncated": False, "row_count": 1},
}


def fake_run(failing_stage: str | None):
    def run(request, on_stage_done):
        trace = []
        failed = False
        for name in STAGE_NAMES:
            if failed:
                result = StageResult(stage=name, status="skipped", reason="earlier stage failed")
            elif name == failing_stage:
                result = StageResult(stage=name, status="failed", reason="simulated failure")
                failed = True
            else:
                result = StageResult(stage=name, status="ok", output=OUTPUTS.get(name, {}))
            trace.append(result)
            on_stage_done(result)
        return trace

    return run


class AskTabTests(unittest.TestCase):
    def test_empty_question_shows_warning(self):
        at = AppTest.from_file(str(APP), default_timeout=60).run()
        at.text_area[0].set_value("")
        at.button[0].click().run()
        self.assertEqual([w.value for w in at.warning], ["Enter a question first."])
        self.assertEqual(list(at.exception), [])

    def test_stage_panels_are_in_pipeline_order(self):
        with mock.patch("src.query.pipeline.run", fake_run(None)):
            at = AppTest.from_file(str(APP), default_timeout=60).run()
            at.text_area[0].set_value("How many orders are from the phone subcategory?")
            at.button[0].click().run()
        self.assertEqual(list(at.exception), [])
        labels = [e.label for e in at.expander]
        self.assertEqual(len(labels), len(STAGE_NAMES))
        for index, name in enumerate(STAGE_NAMES, start=1):
            self.assertTrue(labels[index - 1].startswith(f"{index}. {name}"), labels)

    def test_failed_stage_marks_later_stages_skipped(self):
        with mock.patch("src.query.pipeline.run", fake_run("llm")):
            at = AppTest.from_file(str(APP), default_timeout=60).run()
            at.text_area[0].set_value("How many orders are from the phone subcategory?")
            at.button[0].click().run()
        self.assertEqual(list(at.exception), [])
        labels = [e.label for e in at.expander]
        self.assertIn("4. llm: failed", labels)
        for name in ("plan", "match", "sql_prompt", "sql_generate", "validate", "execute", "respond"):
            self.assertTrue(
                any(label.endswith(f"{name}: skipped") for label in labels),
                f"{name} not skipped: {labels}",
            )

    def test_sql_generator_stream_shows_in_its_own_panel(self):
        def run_streaming(request, on_stage_done):
            request.on_chunk('{"table": "Orders"')
            request.on_sql_chunk('SELECT COUNT(*) FROM "Orders"')
            return []

        with mock.patch("src.query.pipeline.run", run_streaming):
            at = AppTest.from_file(str(APP), default_timeout=60).run()
            at.text_area[0].set_value("How many orders?")
            at.button[0].click().run()
        self.assertEqual(list(at.exception), [])
        captions = [c.value for c in at.caption]
        self.assertIn("8. sql_generate: streaming", captions)
        self.assertIn("4. llm: streaming", captions)
        code = [c.value for c in at.code]
        self.assertIn('SELECT COUNT(*) FROM "Orders"', code)
        self.assertIn('{"table": "Orders"', code)


class RunSqlTabTests(unittest.TestCase):
    def run_sql(self, sql: str) -> AppTest:
        at = AppTest.from_file(str(APP), default_timeout=60).run()
        at.text_area[1].set_value(sql)
        at.button[1].click().run()
        return at

    def test_select_shows_result_table(self):
        at = self.run_sql("SELECT 1 AS n")
        self.assertEqual(list(at.exception), [])
        self.assertEqual(len(at.dataframe), 1)
        self.assertEqual(at.dataframe[0].value.to_dict(orient="records"), [{"n": 1}])

    def test_write_shows_error_and_no_table(self):
        at = self.run_sql("DELETE FROM nothing")
        self.assertEqual(list(at.exception), [])
        self.assertEqual(len(at.dataframe), 0)
        self.assertIn("Only read-only SELECT queries can run here.", [e.value for e in at.error])

    def test_empty_sql_shows_error(self):
        at = self.run_sql("   ")
        self.assertEqual(list(at.exception), [])
        self.assertIn("Enter a SQL query first.", [e.value for e in at.error])


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.evaluation import benchmark_runner as br
from src.evaluation.oracle import ORACLES, run_oracle
from src.query.config import load_registry, select_database
from src.query.observability import Response


def _oracle_result(question_id: str, database_file: Path, drop_last: bool = False, rename_first: bool = False) -> dict:
    spec = ORACLES[question_id]
    frame = run_oracle(spec, database_file)[spec.parts[0].name].copy()
    if drop_last:
        frame = frame.iloc[:-1]
    columns = list(frame.columns)
    if rename_first:
        columns[0] = "renamed_column"
    return {
        "columns": columns,
        "rows": [list(row) for row in frame.itertuples(index=False)],
        "row_count": len(frame),
        "truncated": False,
    }


class ParseQuestionsTests(unittest.TestCase):
    SAMPLE = (
        "### AA01 — First title\n"
        "\n"
        "What is the first\n"
        "question text?\n"
        "\n"
        "**Tags:** x\n"
        "**Caveat:** none\n"
        "\n"
        "---\n"
        "\n"
        "### AA02 — Second title\n"
        "Second question.\n"
        "\n"
        "**Tags:** y\n"
    )

    def test_splits_headers_and_joins_body_lines(self):
        parsed = br.parse_questions(self.SAMPLE)
        self.assertEqual(list(parsed), ["AA01", "AA02"])
        self.assertEqual(parsed["AA01"], ("First title", "What is the first question text?"))
        self.assertEqual(parsed["AA02"], ("Second title", "Second question."))

    def test_body_stops_at_tags_and_caveat_lines(self):
        parsed = br.parse_questions(self.SAMPLE)
        self.assertNotIn("Tags", parsed["AA01"][1])
        self.assertNotIn("Caveat", parsed["AA01"][1])

    def test_real_document_yields_thirty_questions_with_text(self):
        parsed = br.parse_questions(br.QUESTIONS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(len(parsed), 30)
        for qid, (title, text) in parsed.items():
            with self.subTest(qid=qid):
                self.assertTrue(title)
                self.assertTrue(text)
                self.assertNotIn("**Tags:**", text)


class LoadQuestionsTests(unittest.TestCase):
    def test_real_files_load_with_database_and_status(self):
        questions = br.load_questions()
        self.assertEqual(len(questions), 30)
        by_id = {q.id: q for q in questions}
        self.assertEqual(by_id["SS01"].database, "sample_superstore")
        self.assertEqual(by_id["SS01"].manifest_status, "answerable")
        self.assertEqual(by_id["SS13"].manifest_status, "gap")

    def test_mismatched_id_sets_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            questions = tmp_path / "q.md"
            manifest = tmp_path / "m.yaml"
            questions.write_text("### AA01 — Only\nText.\n\n**Tags:** x\n", encoding="utf-8")
            manifest.write_text(
                "questions:\n"
                "  - id: AA01\n    database: sample_superstore\n    status: answerable\n"
                "  - id: AA02\n    database: sample_superstore\n    status: gap\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "AA02"):
                br.load_questions(questions, manifest)


class ClassifyOutcomeTests(unittest.TestCase):
    def test_expected_statuses_are_as_expected(self):
        cases = {
            ("answerable", "ok"): "as_expected",
            ("clarification", "needs_clarification"): "as_expected",
            ("needs_config", "needs_clarification"): "as_expected",
            ("needs_config", "cannot_answer"): "as_expected",
            ("profile_first", "cannot_answer"): "as_expected",
            ("gap", "cannot_answer"): "as_expected",
        }
        for (manifest_status, response_status), expected in cases.items():
            with self.subTest(manifest_status=manifest_status, response_status=response_status):
                self.assertEqual(br.classify_outcome(manifest_status, response_status), expected)

    def test_answerable_not_ok_is_not_answered(self):
        self.assertEqual(br.classify_outcome("answerable", "cannot_answer"), "not_answered")
        self.assertEqual(br.classify_outcome("answerable", "error"), "not_answered")

    def test_ok_for_gap_or_config_is_a_false_answer(self):
        for manifest_status in ("gap", "needs_config", "profile_first", "clarification"):
            with self.subTest(manifest_status=manifest_status):
                self.assertEqual(br.classify_outcome(manifest_status, "ok"), "unexpected_answer")

    def test_other_mismatches_are_unexpected_status(self):
        self.assertEqual(br.classify_outcome("clarification", "cannot_answer"), "unexpected_status")
        self.assertEqual(br.classify_outcome("gap", "rejected"), "unexpected_status")


class JudgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = select_database(load_registry(), "sample_superstore").database_file

    def test_oracle_derived_result_passes(self):
        response = Response(status="ok", result=_oracle_result("SS01", self.db))
        verdict = br.judge("SS01", response, self.db)
        self.assertEqual(verdict["verdict"], "pass")
        self.assertEqual(verdict["matched_part"], ORACLES["SS01"].parts[0].name)
        self.assertEqual(verdict["problems"], [])

    def test_missing_row_is_value_mismatch(self):
        response = Response(status="ok", result=_oracle_result("SS01", self.db, drop_last=True))
        verdict = br.judge("SS01", response, self.db)
        self.assertEqual(verdict["verdict"], "value_mismatch")
        self.assertTrue(verdict["problems"])

    def test_renamed_column_is_column_mismatch(self):
        response = Response(status="ok", result=_oracle_result("SS01", self.db, rename_first=True))
        verdict = br.judge("SS01", response, self.db)
        self.assertEqual(verdict["verdict"], "column_mismatch")
        self.assertTrue(verdict["problems"][0].startswith("columns differ"))

    def test_non_ok_response_is_not_run(self):
        verdict = br.judge("SS01", Response(status="cannot_answer", category="unsupported"), self.db)
        self.assertEqual(verdict, {"verdict": "not_run"})

    def test_question_without_oracle_is_no_oracle(self):
        response = Response(status="ok", result=_oracle_result("SS01", self.db))
        self.assertEqual(br.judge("SS04", response, self.db), {"verdict": "no_oracle"})


class SummarizeTests(unittest.TestCase):
    def test_counts_and_id_lists(self):
        records = [
            {"id": "A", "response_status": "ok", "outcome": "as_expected", "oracle": {"verdict": "pass"}},
            {"id": "B", "response_status": "cannot_answer", "outcome": "not_answered", "oracle": {"verdict": "not_run"}},
            {"id": "C", "response_status": "ok", "outcome": "unexpected_answer", "oracle": {"verdict": "no_oracle"}},
        ]
        summary = br.summarize(records)
        self.assertEqual(summary["questions"], 3)
        self.assertEqual(summary["response_status"], {"ok": 2, "cannot_answer": 1})
        self.assertEqual(summary["verified"], ["A"])
        self.assertEqual(summary["not_answered"], ["B"])
        self.assertEqual(summary["false_or_unexpected"], ["C"])


class RunQuestionTests(unittest.TestCase):
    def test_exception_is_recorded_as_harness_error(self):
        question = br.Question("SS01", "t", "text", "sample_superstore", "answerable")
        with mock.patch.object(br, "run_pipeline", side_effect=RuntimeError("boom")):
            record = br.run_question(question, load_registry(), None, None)
        self.assertEqual(record["outcome"], "harness_error")
        self.assertEqual(record["response_status"], "harness_error")
        self.assertIn("RuntimeError: boom", record["harness_error"])


class OfflineBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = load_registry()
        cls.all_questions = {q.id: q for q in br.load_questions()}

    def _run(self, ids):
        questions = [self.all_questions[i] for i in ids]
        offline = br.OfflineClient()
        report = br.run_benchmark(
            "mocked", questions, self.registry, offline, offline, {"planner": "offline", "sql_generator": "offline"}
        )
        return report, offline

    def test_offline_run_reports_expected_outcomes(self):
        report, offline = self._run(["SS01", "SS04", "SS13"])
        outcomes = {r["id"]: r["outcome"] for r in report["questions"]}
        # The offline planner declines every question, so the answerable question is not answered.
        self.assertEqual(outcomes["SS01"], "not_answered")
        self.assertEqual(outcomes["SS13"], "as_expected")
        self.assertEqual(outcomes["SS04"], "unexpected_status")
        self.assertGreaterEqual(offline.calls, 3)
        self.assertEqual(report["run"]["mode"], "mocked")
        self.assertEqual(report["run"]["models"]["planner"], "offline")

    def test_record_has_expected_fields(self):
        report, _ = self._run(["SS13"])
        record = report["questions"][0]
        for key in (
            "id", "title", "database", "manifest_status", "response_status", "category", "reasons",
            "outcome", "oracle", "sql", "params", "row_count", "wall_ms", "stage_ms", "stages", "result_preview",
        ):
            self.assertIn(key, record)
        self.assertTrue(record["stages"])
        self.assertTrue(all("stage" in stage and "status" in stage for stage in record["stages"]))

    def test_write_report_creates_timestamped_json(self):
        report, _ = self._run(["SS13"])
        with tempfile.TemporaryDirectory() as tmp:
            path = br.write_report(report, Path(tmp) / "nested")
            self.assertTrue(path.exists())
            self.assertRegex(path.name, r"^benchmark-mocked-\d{8}T\d{6}Z\.json$")
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["summary"], report["summary"])


class CliTests(unittest.TestCase):
    def test_unknown_id_is_rejected(self):
        with self.assertRaises(SystemExit) as caught, mock.patch("sys.stderr"):
            br.main(["--mode", "mocked", "--ids", "ZZ99"])
        self.assertEqual(caught.exception.code, 2)

    def test_mocked_cli_writes_report_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("builtins.print"):
            code = br.main(["--mode", "mocked", "--ids", "SS13", "--report-dir", tmp])
            files = list(Path(tmp).glob("benchmark-mocked-*.json"))
        self.assertEqual(code, 0)
        self.assertEqual(len(files), 1)


if __name__ == "__main__":
    unittest.main()

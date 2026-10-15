import json
import unittest

from src.query.analysis import (
    MAX_ANALYSIS_CHARS,
    MAX_MISSING_TERMS,
    AnalysisError,
    parse_analysis,
)


def _reply(**fields):
    return json.dumps(fields)


class ReadyReplyTests(unittest.TestCase):
    def test_ready_reply_keeps_the_analysis_text(self):
        analysis = parse_analysis(_reply(status="ready", analysis="Sum Sales per Region.", clarification=None))
        self.assertEqual(analysis.status, "ready")
        self.assertEqual(analysis.analysis, "Sum Sales per Region.")
        self.assertIsNone(analysis.clarification)

    def test_ready_reply_drops_a_clarification_object(self):
        analysis = parse_analysis(
            _reply(status="ready", analysis="x", clarification={"message": "ignored", "missing": []})
        )
        self.assertIsNone(analysis.clarification)

    def test_ready_reply_needs_analysis_text(self):
        with self.assertRaisesRegex(AnalysisError, "analysis"):
            parse_analysis(_reply(status="ready", analysis="   ", clarification=None))

    def test_json_wrapped_in_prose_or_fence_is_accepted(self):
        text = "Here you go:\n```json\n" + _reply(status="ready", analysis="x", clarification=None) + "\n```"
        self.assertEqual(parse_analysis(text).analysis, "x")

    def test_crlf_line_endings_are_accepted(self):
        text = _reply(status="ready", analysis="x", clarification=None).replace(", ", ",\r\n")
        self.assertEqual(parse_analysis(text).status, "ready")


class NonReadyReplyTests(unittest.TestCase):
    def test_needs_clarification_carries_message_and_missing_terms(self):
        analysis = parse_analysis(
            _reply(
                status="needs_clarification",
                analysis="",
                clarification={"message": "Profit margin is not defined.", "missing": ["profit margin"]},
            )
        )
        self.assertEqual(analysis.status, "needs_clarification")
        self.assertEqual(analysis.clarification.message, "Profit margin is not defined.")
        self.assertEqual(analysis.clarification.missing, ("profit margin",))

    def test_cannot_answer_needs_a_message(self):
        with self.assertRaisesRegex(AnalysisError, "clarification"):
            parse_analysis(_reply(status="cannot_answer", analysis="", clarification=None))

    def test_missing_terms_are_capped(self):
        terms = [f"term {i}" for i in range(MAX_MISSING_TERMS + 5)]
        analysis = parse_analysis(
            _reply(status="needs_clarification", analysis="", clarification={"message": "m", "missing": terms})
        )
        self.assertEqual(len(analysis.clarification.missing), MAX_MISSING_TERMS)


class MalformedReplyTests(unittest.TestCase):
    def assertRejects(self, text, path=None):
        with self.assertRaises(AnalysisError) as ctx:
            parse_analysis(text)
        if path is not None:
            self.assertEqual(ctx.exception.path, path)

    def test_empty_reply(self):
        self.assertRejects("   ", path="$")

    def test_no_json_object(self):
        self.assertRejects("I cannot help with that.", path="$")

    def test_invalid_json(self):
        self.assertRejects('{"status": "ready",', path="$")

    def test_json_array_is_not_an_envelope(self):
        self.assertRejects("[1, 2]", path="$")

    def test_unknown_status(self):
        self.assertRejects(_reply(status="maybe", analysis="x", clarification=None), path="status")

    def test_missing_status(self):
        self.assertRejects(_reply(analysis="x", clarification=None), path="status")

    def test_analysis_must_be_a_string(self):
        self.assertRejects(_reply(status="ready", analysis=["x"], clarification=None), path="analysis")

    def test_overlong_analysis_is_rejected(self):
        text = _reply(status="ready", analysis="x" * (MAX_ANALYSIS_CHARS + 1), clarification=None)
        self.assertRejects(text, path="analysis")

    def test_clarification_must_be_an_object(self):
        self.assertRejects(
            _reply(status="needs_clarification", analysis="", clarification="why"), path="clarification"
        )

    def test_clarification_message_must_be_non_empty(self):
        self.assertRejects(
            _reply(status="cannot_answer", analysis="", clarification={"message": " "}),
            path="clarification.message",
        )

    def test_missing_must_be_a_list_of_strings(self):
        self.assertRejects(
            _reply(
                status="needs_clarification",
                analysis="",
                clarification={"message": "m", "missing": [1]},
            ),
            path="clarification.missing",
        )


if __name__ == "__main__":
    unittest.main()

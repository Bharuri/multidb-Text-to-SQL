import json
import unittest

from src.query.sql_parser import (
    MAX_SQL_OUTPUT_CHARS,
    SqlParseError,
    count_placeholders,
    parse_generated_sql,
)


def obj(sql, params, assumptions):
    return json.dumps({"sql": sql, "params": params, "assumptions": assumptions})


class ParseShapeTests(unittest.TestCase):
    def test_accepts_bare_object(self):
        result = parse_generated_sql(obj("SELECT 1", [], []))
        self.assertEqual(result.sql, "SELECT 1")
        self.assertEqual(result.params, [])
        self.assertEqual(result.assumptions, [])

    def test_accepts_single_fenced_block(self):
        text = "```json\n" + obj("SELECT 1", [], []) + "\n```"
        self.assertEqual(parse_generated_sql(text).sql, "SELECT 1")

    def test_accepts_plain_fence(self):
        text = "```\n" + obj("SELECT 1", [], []) + "\n```"
        self.assertEqual(parse_generated_sql(text).sql, "SELECT 1")

    def test_keeps_params_and_assumptions(self):
        text = obj("SELECT * FROM t WHERE a = ?", ["x"], ["assumed grain"])
        result = parse_generated_sql(text)
        self.assertEqual(result.params, ["x"])
        self.assertEqual(result.assumptions, ["assumed grain"])

    def test_rejects_non_text(self):
        with self.assertRaises(SqlParseError):
            parse_generated_sql(None)

    def test_rejects_empty(self):
        with self.assertRaises(SqlParseError):
            parse_generated_sql("   ")

    def test_rejects_prose_outside_fence(self):
        with self.assertRaises(SqlParseError):
            parse_generated_sql("Here you go:\n```json\n" + obj("SELECT 1", [], []) + "\n```")

    def test_rejects_two_fenced_blocks(self):
        text = "```json\n" + obj("SELECT 1", [], []) + "\n```\n```json\n{}\n```"
        with self.assertRaises(SqlParseError):
            parse_generated_sql(text)

    def test_rejects_array_top_level(self):
        with self.assertRaises(SqlParseError):
            parse_generated_sql("[1, 2]")

    def test_rejects_scalar_top_level(self):
        with self.assertRaises(SqlParseError):
            parse_generated_sql('"SELECT 1"')

    def test_rejects_trailing_content(self):
        with self.assertRaises(SqlParseError):
            parse_generated_sql(obj("SELECT 1", [], []) + " extra")

    def test_rejects_two_objects(self):
        with self.assertRaises(SqlParseError):
            parse_generated_sql(obj("SELECT 1", [], []) + obj("SELECT 2", [], []))

    def test_rejects_invalid_json(self):
        with self.assertRaises(SqlParseError):
            parse_generated_sql('{"sql": "SELECT 1",}')

    def test_rejects_oversized_output(self):
        with self.assertRaises(SqlParseError):
            parse_generated_sql(" " * (MAX_SQL_OUTPUT_CHARS + 1))

    def test_rejects_duplicate_keys(self):
        text = '{"sql": "SELECT 1", "sql": "SELECT 2", "params": [], "assumptions": []}'
        with self.assertRaises(SqlParseError) as ctx:
            parse_generated_sql(text)
        self.assertIn("duplicate", ctx.exception.reason)

    def test_rejects_nan_and_infinity(self):
        for constant in ("NaN", "Infinity", "-Infinity"):
            text = f'{{"sql": "SELECT ?", "params": [{constant}], "assumptions": []}}'
            with self.assertRaises(SqlParseError):
                parse_generated_sql(text)

    def test_category_is_invalid_sql(self):
        self.assertEqual(SqlParseError.category, "invalid_sql")


class KeyTests(unittest.TestCase):
    def test_rejects_missing_key(self):
        with self.assertRaises(SqlParseError):
            parse_generated_sql('{"sql": "SELECT 1", "params": []}')

    def test_rejects_extra_key(self):
        text = '{"sql": "SELECT 1", "params": [], "assumptions": [], "note": "x"}'
        with self.assertRaises(SqlParseError):
            parse_generated_sql(text)


class SqlFieldTests(unittest.TestCase):
    def test_rejects_non_string_sql(self):
        with self.assertRaises(SqlParseError):
            parse_generated_sql('{"sql": 5, "params": [], "assumptions": []}')

    def test_rejects_empty_sql(self):
        with self.assertRaises(SqlParseError):
            parse_generated_sql(obj("   ", [], []))


class ParamTypeTests(unittest.TestCase):
    def test_accepts_scalar_types(self):
        result = parse_generated_sql(
            obj("SELECT ?, ?, ?, ?", ["text", 3, 2.5, True], [])
        )
        self.assertEqual(result.params, ["text", 3, 2.5, True])

    def test_accepts_null(self):
        result = parse_generated_sql(obj("SELECT ?", [None], []))
        self.assertEqual(result.params, [None])

    def test_rejects_nested_list(self):
        with self.assertRaises(SqlParseError):
            parse_generated_sql(obj("SELECT ?", [[1]], []))

    def test_rejects_object_param(self):
        with self.assertRaises(SqlParseError):
            parse_generated_sql(obj("SELECT ?", [{"a": 1}], []))

    def test_rejects_non_list_params(self):
        with self.assertRaises(SqlParseError):
            parse_generated_sql('{"sql": "SELECT 1", "params": {}, "assumptions": []}')

    def test_rejects_float_overflow_to_infinity(self):
        text = '{"sql": "SELECT ?", "params": [1e400], "assumptions": []}'
        with self.assertRaises(SqlParseError):
            parse_generated_sql(text)


class AssumptionTests(unittest.TestCase):
    def test_rejects_non_list_assumptions(self):
        with self.assertRaises(SqlParseError):
            parse_generated_sql('{"sql": "SELECT 1", "params": [], "assumptions": "none"}')

    def test_rejects_non_string_assumption(self):
        with self.assertRaises(SqlParseError):
            parse_generated_sql(obj("SELECT 1", [], [1]))


class PlaceholderCountTests(unittest.TestCase):
    def test_count_matches_params(self):
        result = parse_generated_sql(obj("SELECT * FROM t WHERE a = ? AND b = ?", ["x", "y"], []))
        self.assertEqual(result.params, ["x", "y"])

    def test_rejects_fewer_params_than_placeholders(self):
        with self.assertRaises(SqlParseError) as ctx:
            parse_generated_sql(obj("SELECT ?, ?", ["x"], []))
        self.assertEqual(ctx.exception.path, "params")

    def test_rejects_more_params_than_placeholders(self):
        with self.assertRaises(SqlParseError):
            parse_generated_sql(obj("SELECT ?", ["x", "y"], []))

    def test_question_mark_in_string_literal_is_not_a_placeholder(self):
        self.assertEqual(count_placeholders("SELECT 'what?' AS q, ?"), 1)

    def test_question_mark_in_escaped_literal_is_not_a_placeholder(self):
        self.assertEqual(count_placeholders("SELECT 'it''s ?' , ?"), 1)

    def test_question_mark_in_quoted_identifier_is_not_a_placeholder(self):
        self.assertEqual(count_placeholders('SELECT "col?" FROM t WHERE x = ?'), 1)

    def test_question_mark_in_line_comment_is_not_a_placeholder(self):
        self.assertEqual(count_placeholders("SELECT ? -- really?\nFROM t"), 1)

    def test_question_mark_in_block_comment_is_not_a_placeholder(self):
        self.assertEqual(count_placeholders("SELECT /* ? */ ?"), 1)

    def test_question_mark_in_dollar_quoted_string_is_not_a_placeholder(self):
        self.assertEqual(count_placeholders("SELECT $$?$$, $t$ ? $t$, ?"), 1)

    def test_question_mark_in_e_string_with_backslash_escape(self):
        self.assertEqual(count_placeholders(r"SELECT E'it\'s ?', ?"), 1)

    def test_backslash_is_literal_in_standard_string(self):
        self.assertEqual(count_placeholders(r"SELECT 'a\', ?"), 1)

    def test_dollar_sign_identifier_is_not_a_dollar_quote(self):
        self.assertEqual(count_placeholders("SELECT a$b, ?"), 1)

    def test_unterminated_string_rejected(self):
        with self.assertRaises(SqlParseError):
            count_placeholders("SELECT 'abc")

    def test_unterminated_quoted_identifier_rejected(self):
        with self.assertRaises(SqlParseError):
            count_placeholders('SELECT "abc')

    def test_unterminated_block_comment_rejected(self):
        with self.assertRaises(SqlParseError):
            count_placeholders("SELECT 1 /* open")

    def test_unterminated_dollar_quote_rejected(self):
        with self.assertRaises(SqlParseError):
            count_placeholders("SELECT $t$ open")

    def test_placeholder_inside_literal_does_not_fool_mismatch_check(self):
        with self.assertRaises(SqlParseError):
            parse_generated_sql(obj("SELECT 'a?' AS x", ["unused"], []))


if __name__ == "__main__":
    unittest.main()

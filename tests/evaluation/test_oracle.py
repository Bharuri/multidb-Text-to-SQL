from __future__ import annotations

import unittest

import pandas as pd

from src.evaluation.oracle import (
    ORACLES,
    compare_frames,
    run_oracle,
)
from src.query.config import load_registry, select_database
from tests.evaluation.pandas_reference import REFERENCES, connect


class OracleIdTests(unittest.TestCase):
    def test_every_oracle_has_a_pandas_reference(self) -> None:
        self.assertEqual(set(REFERENCES), set(ORACLES))

    def test_oracle_database_matches_registry(self) -> None:
        registry = load_registry()
        for qid, spec in ORACLES.items():
            with self.subTest(qid=qid):
                self.assertIn(spec.database, registry.databases)
                self.assertEqual(spec.question_id, qid)


class OracleCrossCheckTests(unittest.TestCase):
    """Each DuckDB oracle must agree with an independent pandas recomputation."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = load_registry()

    def _check(self, qid: str) -> None:
        spec = ORACLES[qid]
        db_path = select_database(self.registry, spec.database).database_file
        actual = run_oracle(spec, db_path)
        con = connect(db_path)
        try:
            expected = REFERENCES[qid](con)
        finally:
            con.close()
        self.assertEqual(set(expected), {p.name for p in spec.parts})
        for part in spec.parts:
            with self.subTest(qid=qid, part=part.name):
                problems = compare_frames(
                    expected[part.name], actual[part.name], key_columns=part.key_columns
                )
                self.assertEqual(problems, [])


def _make_cross_check(qid: str):
    def test(self: OracleCrossCheckTests) -> None:
        self._check(qid)

    test.__name__ = f"test_{qid.lower()}_matches_pandas"
    return test


for _qid in sorted(ORACLES):
    setattr(OracleCrossCheckTests, f"test_{_qid.lower()}_matches_pandas", _make_cross_check(_qid))


class DataInvariantTests(unittest.TestCase):
    """Facts about the source data that the oracle interpretations rely on."""

    @classmethod
    def setUpClass(cls) -> None:
        registry = load_registry()
        cls.superstore = select_database(registry, "sample_superstore").database_file
        cls.netflix = select_database(registry, "netflix_titles").database_file

    def test_superstore_order_id_maps_to_one_order_date(self) -> None:
        con = connect(self.superstore)
        try:
            n = con.execute(
                "SELECT COUNT(*) FROM (SELECT Order_ID FROM Orders "
                "GROUP BY Order_ID HAVING COUNT(DISTINCT CAST(Order_Date AS DATE)) > 1)"
            ).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(n, 0)

    def test_netflix_has_junk_rows_excluded_by_type_filter(self) -> None:
        con = connect(self.netflix)
        try:
            junk = con.execute(
                "SELECT COUNT(*) FROM netflix_titles WHERE type NOT IN ('Movie', 'TV Show') OR type IS NULL"
            ).fetchone()[0]
        finally:
            con.close()
        self.assertGreater(junk, 0)


class CompareFramesTests(unittest.TestCase):
    def test_identical_frames_have_no_problems(self) -> None:
        f = pd.DataFrame({"k": ["a", "b"], "v": [1.0, 2.0]})
        self.assertEqual(compare_frames(f, f.copy(), key_columns=("k",)), [])

    def test_column_order_difference_is_reported(self) -> None:
        a = pd.DataFrame({"k": [1], "v": [2]})
        b = pd.DataFrame({"v": [2], "k": [1]})
        problems = compare_frames(a, b, key_columns=("k",))
        self.assertTrue(problems and "columns differ" in problems[0])

    def test_row_count_difference_is_reported(self) -> None:
        a = pd.DataFrame({"k": [1, 2], "v": [1, 1]})
        b = pd.DataFrame({"k": [1], "v": [1]})
        problems = compare_frames(a, b, key_columns=("k",))
        self.assertTrue(problems and "row count" in problems[0])

    def test_value_mismatch_is_reported_with_example(self) -> None:
        a = pd.DataFrame({"k": [1, 2], "v": [1.0, 2.0]})
        b = pd.DataFrame({"k": [1, 2], "v": [1.0, 9.0]})
        problems = compare_frames(a, b, key_columns=("k",))
        self.assertEqual(len(problems), 1)
        self.assertIn("v: 1 mismatches", problems[0])

    def test_rows_are_matched_by_key_not_position(self) -> None:
        a = pd.DataFrame({"k": [1, 2], "v": [10.0, 20.0]})
        b = pd.DataFrame({"k": [2, 1], "v": [20.0, 10.0]})
        self.assertEqual(compare_frames(a, b, key_columns=("k",)), [])

    def test_tolerance_absorbs_float_noise(self) -> None:
        a = pd.DataFrame({"k": [1], "v": [1.0]})
        b = pd.DataFrame({"k": [1], "v": [1.0 + 1e-12]})
        self.assertEqual(compare_frames(a, b, key_columns=("k",)), [])

    def test_null_position_mismatch_is_reported(self) -> None:
        a = pd.DataFrame({"k": [1, 2], "v": [1.0, None]})
        b = pd.DataFrame({"k": [1, 2], "v": [1.0, 3.0]})
        problems = compare_frames(a, b, key_columns=("k",))
        self.assertTrue(problems and "NULL positions" in problems[0])

    def test_duplicate_keys_are_reported(self) -> None:
        a = pd.DataFrame({"k": [1, 1], "v": [1, 2]})
        problems = compare_frames(a, a.copy(), key_columns=("k",))
        self.assertTrue(problems and "duplicate keys" in problems[0])


if __name__ == "__main__":
    unittest.main()

import json
import unittest
from pathlib import Path
from unittest import mock

from src.query import pipeline
from src.query.catalog import resolve
from src.query.llm import SqlReply
from src.query.pipeline import Request, final_status, run

_CATALOG = resolve("sample_superstore")
_STEM = Path(_CATALOG.db_file).stem
_SCHEMA = _CATALOG.physical["schema"]
ORDERS = f'"{_STEM}"."{_SCHEMA}"."Orders"'
REAL_EXECUTE = pipeline.execute


def plan(**overrides) -> dict:
    base = {
        "table": "Orders",
        "select": [],
        "dimensions": [],
        "metrics": [],
        "filters": [],
        "group_by": [],
        "having": [],
        "order_by": [],
        "calculations": [],
        "limit": None,
    }
    base.update(overrides)
    return base


GROUPED_PLAN = plan(
    dimensions=["Region", "Category"],
    metrics=[{"function": "SUM", "column": "Sales", "alias": "total"}, {"function": "COUNT", "column": "*", "alias": "n"}],
    group_by=["Region", "Category"],
    having=[{"metric": "total", "operator": ">=", "value": 1000}],
    order_by=[{"ref": "total", "direction": "DESC"}],
    limit=5,
)
GROUPED_SQL = (
    f'SELECT "Region", "Category", SUM("Sales") AS "total", COUNT(*) AS "n" FROM {ORDERS} '
    'GROUP BY "Region", "Category" HAVING SUM("Sales") >= ? ORDER BY "total" DESC LIMIT 5'
)


def planner(reply_obj) -> mock.Mock:
    text = reply_obj if isinstance(reply_obj, str) else json.dumps(reply_obj)
    return mock.Mock(return_value=text)


def generator(sql: str, params: list | None = None) -> mock.Mock:
    def call(prompt: str, **kwargs) -> SqlReply:
        payload = json.dumps({"sql": sql, "params": params or []})
        return SqlReply(raw=payload, sql=sql, params=params or [])

    return mock.Mock(side_effect=call)


class _PipelineCase(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch("src.query.pipeline._attach_log_file")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.execute_spy = mock.patch("src.query.pipeline.execute", wraps=REAL_EXECUTE)
        self.execute_mock = self.execute_spy.start()
        self.addCleanup(self.execute_spy.stop)

    def _run(self, reply_obj, generate_mock: mock.Mock | None = None, question: str = "q"):
        llm = planner(reply_obj)
        gen = generate_mock or generator("SELECT 1")
        with mock.patch("src.query.pipeline.complete", llm), mock.patch("src.query.pipeline.generate_sql", gen):
            trace = run(Request(database="sample_superstore", question=question), on_stage_done=lambda r: None)
        return trace, llm, gen

    def _stage(self, trace, name):
        return next(r for r in trace if r.stage == name)


class GroupedQueryTests(_PipelineCase):
    def test_multi_dimension_multi_metric_having_and_order_run_end_to_end(self) -> None:
        trace, llm, gen = self._run(GROUPED_PLAN, generator(GROUPED_SQL, [1000]), question="Sales and order count by region and category")
        self.assertEqual(final_status(trace), "ok")
        self.assertEqual(self._stage(trace, "sql_generate").output["sql"], GROUPED_SQL)
        self.execute_mock.assert_called_once()
        rows = self._stage(trace, "execute").output["rows"]
        self.assertLessEqual(len(rows), 5)
        totals = [row[2] for row in rows]
        self.assertTrue(all(total >= 1000 for total in totals))
        self.assertEqual(totals, sorted(totals, reverse=True))

    def test_the_same_plan_and_metadata_give_the_same_decisions(self) -> None:
        first, _, _ = self._run(GROUPED_PLAN, generator(GROUPED_SQL, [1000]))
        second, _, _ = self._run(GROUPED_PLAN, generator(GROUPED_SQL, [1000]))
        self.assertEqual(
            [(r.stage, r.status, r.reason) for r in first],
            [(r.stage, r.status, r.reason) for r in second],
        )
        self.assertEqual(self._stage(first, "sql_generate").output, self._stage(second, "sql_generate").output)
        self.assertEqual(self._stage(first, "execute").output["rows"], self._stage(second, "execute").output["rows"])


class SimpleQueryTests(_PipelineCase):
    def test_simple_row_question_still_works(self) -> None:
        row_plan = plan(
            select=["Order_ID", "Region", "Sales"],
            filters=[{"column": "Region", "operator": "=", "value": "West"}],
            limit=3,
        )
        sql = f'SELECT "Order_ID", "Region", "Sales" FROM {ORDERS} WHERE LOWER("Region") = LOWER(?) LIMIT 3'
        trace, _, _ = self._run(row_plan, generator(sql, ["West"]))
        self.assertEqual(final_status(trace), "ok")
        self.assertLessEqual(self._stage(trace, "execute").output["row_count"], 3)


class CannotAnswerTests(_PipelineCase):
    def test_cannot_answer_reply_never_reaches_sql_or_execution(self) -> None:
        trace, llm, gen = self._run({"cannot_answer": True, "reason": "No approved definition for profit per visit."})
        self.assertEqual(final_status(trace), "cannot_answer")
        gen.assert_not_called()
        self.execute_mock.assert_not_called()
        self.assertEqual(llm.call_count, 1)

    def test_unknown_column_is_rejected_before_sql_generation(self) -> None:
        bad = plan(metrics=[{"function": "SUM", "column": "Revenue_Per_Visit", "alias": "rpv"}])
        trace, llm, gen = self._run(bad)
        self.assertEqual(final_status(trace), "rejected")
        self.assertEqual(self._stage(trace, "match").status, "failed")
        gen.assert_not_called()
        self.execute_mock.assert_not_called()
        self.assertEqual(llm.call_count, 1)

    def test_unknown_table_is_rejected_before_sql_generation(self) -> None:
        bad = plan(table="Secrets", metrics=[{"function": "COUNT", "column": "*", "alias": "n"}])
        trace, _, gen = self._run(bad)
        self.assertEqual(final_status(trace), "rejected")
        gen.assert_not_called()
        self.execute_mock.assert_not_called()


class UnsafeSqlTests(_PipelineCase):
    """The planner is valid here; the SQL generator returns unsafe or unknown SQL. Validation must stop it before execution, and no second call is made."""

    UNSAFE = {
        "delete": f"DELETE FROM {ORDERS} WHERE 1 = 1",
        "drop": f"SELECT COUNT(*) AS n FROM {ORDERS}; DROP TABLE {ORDERS}",
        "join": f'SELECT COUNT(*) AS n FROM {ORDERS} o JOIN "{_STEM}"."{_SCHEMA}"."Returns" r ON o."Order_ID" = r."Order_ID"',
        "unknown_column": f'SELECT SUM("Revenue_Per_Visit") AS n FROM {ORDERS}',
        "unknown_table": f'SELECT COUNT(*) AS n FROM "{_STEM}"."{_SCHEMA}"."Secrets"',
        "text_equality_without_lower": f'SELECT COUNT(*) AS n FROM {ORDERS} WHERE "Sub_Category" = ?',
    }

    def test_unsafe_sql_never_executes_and_is_not_retried(self) -> None:
        count_plan = plan(metrics=[{"function": "COUNT", "column": "*", "alias": "n"}])
        for label, sql in self.UNSAFE.items():
            with self.subTest(label):
                self.execute_mock.reset_mock()
                trace, llm, gen = self._run(count_plan, generator(sql, ["Phones"]))
                self.assertEqual(final_status(trace), "rejected")
                self.assertEqual(self._stage(trace, "validate").status, "failed")
                self.assertEqual(self._stage(trace, "execute").status, "skipped")
                self.execute_mock.assert_not_called()
                self.assertEqual(gen.call_count, 1)
                self.assertEqual(llm.call_count, 1)


class DeterminismTests(_PipelineCase):
    def test_rejected_plan_gives_the_same_reasons_each_time(self) -> None:
        bad = plan(metrics=[{"function": "SUM", "column": "Revenue_Per_Visit", "alias": "rpv"}])
        first, _, _ = self._run(bad)
        second, _, _ = self._run(bad)
        self.assertEqual(self._stage(first, "match").reason, self._stage(second, "match").reason)


if __name__ == "__main__":
    unittest.main()

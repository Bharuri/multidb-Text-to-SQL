import dataclasses
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import duckdb

from src.query import pipeline
from src.query.catalog import Catalog, ColumnInfo, TableInfo
from src.query.config import (
    ConfigError,
    DatabaseEntry,
    MetadataMismatchError,
    MissingFileError,
    UnknownDatabaseError,
    load_registry,
)
from src.query.llm import LlmError
from src.query.observability import Response


def _column(name, type_="VARCHAR"):
    return ColumnInfo(name=name, type=type_, nullable=False, has_nulls=False, is_unique=False)


def _analysis_json(status="ready", analysis="Total sales per region.", clarification=None):
    return json.dumps({"status": status, "analysis": analysis, "clarification": clarification})


GOOD_SQL = "SELECT region, sum(sales) AS total FROM orders GROUP BY region"


def _sql_json(sql=GOOD_SQL, params=None, assumptions=None):
    return json.dumps({"sql": sql, "params": params or [], "assumptions": assumptions or []})


class FakeClient:
    def __init__(self, text=None, error=None):
        self.text = text
        self.error = error
        self.prompts = []

    def complete(self, prompt, on_chunk=None):
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        return self.text


class SequenceClient:
    """Double that returns or raises one scripted reply per call."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def complete(self, prompt, on_chunk=None):
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class PipelineFixture:
    """Shared temp-database and patching setup for pipeline test classes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "shop.duckdb")
        connection = duckdb.connect(self.db_path)
        connection.execute("CREATE TABLE orders (order_id VARCHAR, region VARCHAR, sales DOUBLE)")
        connection.execute("INSERT INTO orders VALUES ('o1', 'West', 10), ('o2', 'East', 20), ('o3', 'West', 30)")
        connection.close()

        self.registry = load_registry()
        self.catalog = Catalog(
            key="shop",
            schema="shop",
            database_file=self.db_path,
            tables={
                "orders": TableInfo(
                    name="orders",
                    source_sheet="orders",
                    row_count=3,
                    columns={c: _column(c) for c in ("order_id", "region", "sales")},
                )
            },
            semantic_columns=(),
            relationships=(),
            numeric_operators=(),
            fingerprint=(),
            metrics=(),
        )
        self.entry = DatabaseEntry(
            key="shop",
            physical_mapping=Path(self.db_path),
            semantic_mapping=Path(self.db_path),
            database_file=Path(self.db_path),
            database_file_setting="",
            business_mapping=None,
        )

        patches = [
            mock.patch.object(pipeline, "select_database", side_effect=self._select),
            mock.patch.object(pipeline, "load_catalog", side_effect=lambda registry, key: self.catalog),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _select(self, registry, key):
        if key != "shop":
            raise UnknownDatabaseError(f"Unknown database {key!r}.")
        return self.entry

    def run_with(
        self,
        planner_text=None,
        sql_text=None,
        planner_error=None,
        sql_error=None,
        question="Total sales by region?",
    ):
        return pipeline.run_pipeline(
            question,
            "shop",
            registry=self.registry,
            planner=FakeClient(text=planner_text, error=planner_error),
            sql_generator=FakeClient(text=sql_text, error=sql_error),
            request_id="test-request",
        )

    def ready_run(self, **kwargs):
        return self.run_with(planner_text=_analysis_json(), sql_text=_sql_json(), **kwargs)

    def stage(self, response, name):
        for result in response.trace:
            if result.stage == name:
                return result
        self.fail(f"stage {name} missing from trace")

    def assertFailsAt(self, response, stage, category, status=None):
        self.assertEqual(response.category, category, response)
        failed = [s for s in response.trace if s.status == "failed"]
        self.assertEqual(len(failed), 1, response)
        self.assertEqual(failed[0].stage, stage)
        self.assertEqual(failed[0].category, category)
        self.assertEqual(response.status, status or pipeline.RESPONSE_STATUS_BY_CATEGORY[category])
        names = [s.stage for s in response.trace]
        for name in names[names.index(stage) + 1 :]:
            self.assertEqual(self.stage(response, name).status, "skipped")


class PipelineTests(PipelineFixture, unittest.TestCase):
    def test_success_runs_every_stage_in_order(self):
        response = self.ready_run()
        self.assertEqual(response.status, "ok", response)
        self.assertIsNone(response.category)
        self.assertEqual([s.stage for s in response.trace], list(pipeline.STAGES))
        self.assertTrue(all(s.status == "ok" for s in response.trace))
        self.assertEqual(response.result["row_count"], 2)
        self.assertEqual(sorted(response.result["columns"]), ["region", "total"])
        self.assertEqual(response.sql, GOOD_SQL)
        self.assertEqual(response.params, ())

    def test_success_rows_are_not_in_trace(self):
        response = self.ready_run()
        for result in response.trace:
            self.assertNotIn("rows", result.output)
        self.assertEqual(len(response.result["rows"]), 2)

    def test_success_result_shape_is_passed_through(self):
        response = self.ready_run()
        self.assertIsInstance(response, Response)
        self.assertEqual(set(response.result), {"columns", "rows", "row_count", "truncated"})
        self.assertFalse(response.result["truncated"])

    def test_empty_question_fails_at_submit(self):
        response = self.run_with(question="   ")
        self.assertFailsAt(response, "submit", "invalid_request")

    def test_unknown_database_fails_at_resolve(self):
        planner = FakeClient(text=_analysis_json())
        response = pipeline.run_pipeline(
            "Total sales?", "nope", registry=self.registry, planner=planner, sql_generator=FakeClient(text=_sql_json())
        )
        self.assertFailsAt(response, "resolve_database", "unknown_database")
        self.assertEqual(planner.prompts, [])

    def test_missing_database_file_maps_to_metadata_error(self):
        with mock.patch.object(pipeline, "select_database", side_effect=MissingFileError("shop: DuckDB file not found.")):
            response = self.ready_run()
        self.assertFailsAt(response, "resolve_database", "metadata_error")

    def test_metadata_mismatch_maps_to_metadata_error(self):
        with mock.patch.object(pipeline, "select_database", side_effect=MetadataMismatchError("shop: mismatch")):
            response = self.ready_run()
        self.assertFailsAt(response, "resolve_database", "metadata_error")

    def test_catalog_load_error_maps_to_metadata_error(self):
        with mock.patch.object(pipeline, "load_catalog", side_effect=ConfigError("shop: broken catalog")):
            response = self.ready_run()
        self.assertFailsAt(response, "load_catalog", "metadata_error")

    def test_context_over_budget_is_unsupported(self):
        tight = dataclasses.replace(
            self.registry,
            retrieval=dataclasses.replace(self.registry.retrieval, max_prompt_chars=5),
        )
        planner = FakeClient(text=_analysis_json())
        response = pipeline.run_pipeline(
            "Total sales by region?", "shop", registry=tight, planner=planner, sql_generator=FakeClient(text=_sql_json())
        )
        failed = next(s.stage for s in response.trace if s.status == "failed")
        self.assertIn(failed, ("retrieve_context", "build_planner_prompt"))
        self.assertFailsAt(response, failed, "unsupported")
        self.assertEqual(planner.prompts, [])

    def test_unexpected_exception_maps_to_internal_error_without_message(self):
        secret = "credential-like-text-that-must-not-leak"
        with mock.patch.object(pipeline, "build_planner_prompt", side_effect=RuntimeError(secret)):
            response = self.ready_run()
        self.assertFailsAt(response, "build_planner_prompt", "internal_error")
        self.assertNotIn(secret, self.stage(response, "build_planner_prompt").reason)
        self.assertNotIn(secret, json.dumps(response.to_dict()))

    def test_planner_llm_error_maps_to_llm_error(self):
        response = self.run_with(planner_error=LlmError("Bedrock call failed: ClientError"), sql_text=_sql_json())
        self.assertFailsAt(response, "planner_llm", "llm_error")
        self.assertIn("Bedrock call failed", self.stage(response, "planner_llm").reason)

    def test_sql_generator_llm_error_maps_to_llm_error(self):
        response = self.run_with(planner_text=_analysis_json(), sql_error=LlmError("throttled"))
        self.assertEqual(response.category, "llm_error", response)
        self.assertEqual(response.status, "error")

    def test_on_stage_receives_full_trace_in_order_on_failure(self):
        seen = []
        response = pipeline.run_pipeline(
            "Total sales by region?",
            "shop",
            registry=self.registry,
            planner=FakeClient(error=LlmError("boom")),
            sql_generator=FakeClient(text=_sql_json()),
            on_stage=seen.append,
        )
        self.assertEqual([s.stage for s in seen], [s.stage for s in response.trace])
        self.assertEqual([s.stage for s in seen], list(pipeline.STAGES))


class PlannerEnvelopeTests(PipelineFixture, unittest.TestCase):
    def run_planner(self, replies):
        planner = SequenceClient(replies)
        response = pipeline.run_pipeline(
            "Total sales by region?",
            "shop",
            registry=self.registry,
            planner=planner,
            sql_generator=FakeClient(text=_sql_json()),
            request_id="test-request",
        )
        return response, planner

    def test_invalid_envelope_is_repaired_once(self):
        response, planner = self.run_planner(["not json", _analysis_json()])
        self.assertEqual(response.status, "ok", response)
        self.assertEqual(len(planner.prompts), 2)
        repair = planner.prompts[1]
        self.assertIn("Your previous reply:\nnot json", repair)
        self.assertIn("It was rejected because:", repair)
        self.assertIn("Reply again with one complete corrected JSON object", repair)

    def test_repair_that_is_still_invalid_is_rejected_at_parse(self):
        response, planner = self.run_planner(["not json", "still not json"])
        self.assertFailsAt(response, "parse_analysis", "invalid_plan")
        self.assertEqual(len(planner.prompts), 2)

    def test_repair_is_skipped_when_repair_prompt_is_too_large(self):
        oversized = "x" * (self.registry.retrieval.max_prompt_chars + 1)
        with mock.patch.object(pipeline, "build_repair_prompt", return_value=oversized):
            response, planner = self.run_planner(["not json", _analysis_json()])
        self.assertFailsAt(response, "parse_analysis", "invalid_plan")
        self.assertEqual(len(planner.prompts), 1)

    def test_cannot_answer_is_not_retried(self):
        reply = _analysis_json(
            status="cannot_answer",
            analysis="",
            clarification={"message": "The data does not cover this."},
        )
        response, planner = self.run_planner([reply, _analysis_json()])
        self.assertFailsAt(response, "parse_analysis", "unsupported", status="cannot_answer")
        self.assertEqual(self.stage(response, "parse_analysis").reason, "The data does not cover this.")
        self.assertEqual(len(planner.prompts), 1)

    def test_needs_clarification_stops_with_message_and_missing(self):
        reply = json.dumps(
            {
                "status": "needs_clarification",
                "analysis": "",
                "clarification": {"message": "Which region column?", "missing": ["region"]},
            }
        )
        response = self.run_with(planner_text=reply, sql_text=_sql_json())
        self.assertFailsAt(response, "parse_analysis", "clarification_needed", status="needs_clarification")
        failed = self.stage(response, "parse_analysis")
        self.assertEqual(failed.reason, "Which region column?")
        self.assertEqual(failed.output["missing"], ["region"])
        self.assertEqual(response.reasons, ("Which region column?",))

    def test_llm_error_is_not_retried(self):
        response, planner = self.run_planner([LlmError("throttled"), _analysis_json()])
        self.assertEqual(response.category, "llm_error", response)
        self.assertEqual(len(planner.prompts), 1)

    def test_analysis_text_reaches_sql_prompt(self):
        response = self.run_with(
            planner_text=_analysis_json(analysis="Sum sales grouped by region."),
            sql_text=_sql_json(),
        )
        self.assertEqual(response.status, "ok", response)
        sql_prompt = self.stage(response, "build_sql_prompt").detail["sql_prompt"]
        self.assertIn("Sum sales grouped by region.", sql_prompt)


class SqlRepairTests(PipelineFixture, unittest.TestCase):
    INLINE_SQL = "SELECT region, sum(sales) AS total FROM orders WHERE region = 'East' GROUP BY region"
    PARAM_SQL = "SELECT region, sum(sales) AS total FROM orders WHERE region = ? GROUP BY region"

    def run_sql(self, replies):
        generator = SequenceClient(replies)
        response = pipeline.run_pipeline(
            "Total sales for East?",
            "shop",
            registry=self.registry,
            planner=FakeClient(text=_analysis_json()),
            sql_generator=generator,
            request_id="test-request",
        )
        return response, generator

    def test_inline_string_passes_without_repair(self):
        response, generator = self.run_sql([_sql_json(self.INLINE_SQL)])
        self.assertEqual(response.status, "ok", response)
        self.assertEqual(len(generator.prompts), 1)
        self.assertEqual(self.stage(response, "validate_sql").output["attempts"], 1)

    def test_near_match_column_is_rewritten_without_repair(self):
        sql = 'SELECT region, sum("sales") AS total FROM orders WHERE "order id" IS NOT NULL GROUP BY region'
        response, generator = self.run_sql([_sql_json(sql)])
        self.assertEqual(response.status, "ok", response)
        self.assertEqual(len(generator.prompts), 1)
        self.assertEqual(
            self.stage(response, "validate_sql").detail["column_rewrites"],
            [{"written": "order id", "physical": "order_id"}],
        )

    def test_inline_number_is_allowed_without_repair(self):
        sql = "SELECT region, sum(sales) AS total FROM orders WHERE sales > 10 GROUP BY region"
        response, generator = self.run_sql([_sql_json(sql)])
        self.assertEqual(response.status, "ok", response)
        self.assertEqual(len(generator.prompts), 1)

    def test_multiple_statements_are_repaired_once(self):
        bad = "SELECT region FROM orders; SELECT sales FROM orders"
        response, generator = self.run_sql([_sql_json(bad), _sql_json(GOOD_SQL)])
        self.assertEqual(response.status, "ok", response)
        self.assertEqual(len(generator.prompts), 2)
        self.assertIn("exactly one SQL statement is allowed", generator.prompts[1])

    def test_runtime_error_is_repaired_once_with_the_database_message(self):
        bad = "SELECT region, sum(sales) AS total FROM orders WHERE CAST(region AS INTEGER) > 1 GROUP BY region"
        response, generator = self.run_sql([_sql_json(bad), _sql_json(GOOD_SQL)])
        self.assertEqual(response.status, "ok", response)
        self.assertEqual(len(generator.prompts), 2)
        self.assertIn("query failed at runtime", generator.prompts[1])
        self.assertIn("ConversionException", generator.prompts[1])
        self.assertEqual(self.stage(response, "execute").status, "ok")

    def test_runtime_error_is_not_repaired_twice(self):
        bad = "SELECT region, sum(sales) AS total FROM orders WHERE CAST(region AS INTEGER) > 1 GROUP BY region"
        response, generator = self.run_sql([_sql_json(bad), _sql_json(bad)])
        self.assertFailsAt(response, "execute", "execution_error", status="error")
        self.assertEqual(len(generator.prompts), 2)
        failed = self.stage(response, "execute")
        self.assertEqual(failed.reason, "the query failed at runtime")
        self.assertNotIn("ConversionException", failed.reason)

    def test_write_statement_is_not_repaired(self):
        bad = "SELECT region FROM orders; DROP TABLE orders"
        response, generator = self.run_sql([_sql_json(bad), _sql_json(GOOD_SQL)])
        self.assertEqual(response.status, "rejected", response)
        self.assertEqual(len(generator.prompts), 1)

    def test_unknown_column_is_repaired_once(self):
        bad = "SELECT region, sum(profit) AS total FROM orders GROUP BY region"
        response, generator = self.run_sql([_sql_json(bad), _sql_json(GOOD_SQL)])
        self.assertEqual(response.status, "ok", response)
        self.assertEqual(len(generator.prompts), 2)

    def test_placeholder_count_mismatch_is_repaired_once(self):
        mismatched = _sql_json(self.PARAM_SQL, params=["East", "extra"])
        response, generator = self.run_sql([mismatched, _sql_json(self.PARAM_SQL, params=["East"])])
        self.assertEqual(response.status, "ok", response)
        self.assertEqual(len(generator.prompts), 2)
        self.assertIn("Your previous reply:", generator.prompts[1])

    def test_placeholder_count_mismatch_that_repeats_is_rejected(self):
        mismatched = _sql_json(self.PARAM_SQL, params=["East", "extra"])
        response, generator = self.run_sql([mismatched, mismatched])
        self.assertEqual(response.category, "invalid_sql", response)
        self.assertEqual(response.status, "rejected")
        self.assertEqual(len(generator.prompts), 2)


if __name__ == "__main__":
    unittest.main()

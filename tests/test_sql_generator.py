import dataclasses
import unittest
from types import SimpleNamespace

from src.query.config import LlmSettings
from src.query.llm import BedrockClient, LlmError
from src.query.sql_generator import (
    SQL_GENERATE_STAGE,
    build_sql_generator,
    generate_sql,
)

PLANNER = LlmSettings(
    provider="bedrock", model_id="planner-model", region="us-east-1", temperature=0.0, max_output_tokens=100
)
GENERATOR = dataclasses.replace(PLANNER, model_id="sql-model", max_output_tokens=500)
GENERATED = '{"sql": "SELECT 1", "params": [], "assumptions": []}'


class FakeRuntime:
    def __init__(self, text=GENERATED, fail=None):
        self.text = text
        self.fail = fail
        self.calls = []

    def converse(self, **request):
        self.calls.append(request)
        if self.fail:
            raise self.fail
        return {"output": {"message": {"content": [{"text": self.text}]}}}

    def converse_stream(self, **request):
        self.calls.append(request)
        if self.fail:
            raise self.fail
        parts = [self.text[:10], self.text[10:]]
        events = [{"contentBlockDelta": {"delta": {"text": part}}} for part in parts]
        return {"stream": iter(events)}


def _client(settings, runtime):
    return BedrockClient(settings, runtime=runtime)


class GenerateSqlTests(unittest.TestCase):
    def test_returns_raw_text_and_ok_stage(self):
        runtime = FakeRuntime()
        text, stage = generate_sql(_client(GENERATOR, runtime), "prompt")
        self.assertEqual(text, GENERATED)
        self.assertEqual(stage.stage, SQL_GENERATE_STAGE)
        self.assertEqual(stage.status, "ok")
        self.assertEqual(stage.output, {"characters": len(GENERATED)})

    def test_trace_does_not_contain_generated_sql_or_prompt(self):
        _, stage = generate_sql(_client(GENERATOR, FakeRuntime()), "PROMPT-BODY")
        rendered = repr(stage.to_dict())
        self.assertNotIn("SELECT 1", rendered)
        self.assertNotIn("PROMPT-BODY", rendered)

    def test_streaming_forwards_chunks_and_returns_full_text(self):
        chunks = []
        text, stage = generate_sql(_client(GENERATOR, FakeRuntime()), "prompt", on_chunk=chunks.append)
        self.assertEqual(text, GENERATED)
        self.assertEqual("".join(chunks), GENERATED)
        self.assertEqual(stage.status, "ok")

    def test_provider_failure_is_failed_llm_error_without_provider_message(self):
        runtime = FakeRuntime(fail=RuntimeError("secret request detail"))
        text, stage = generate_sql(_client(GENERATOR, runtime), "prompt")
        self.assertEqual(text, "")
        self.assertEqual(stage.status, "failed")
        self.assertEqual(stage.category, "llm_error")
        self.assertNotIn("secret request detail", stage.reason)

    def test_failure_is_reported_as_stage_not_raised(self):
        runtime = FakeRuntime(fail=RuntimeError("boom"))
        _, stage = generate_sql(_client(GENERATOR, runtime), "prompt")
        self.assertIsInstance(stage.to_dict(), dict)


class BuildSqlGeneratorTests(unittest.TestCase):
    def _registry(self, generator_provider="bedrock"):
        return SimpleNamespace(
            planner_llm=PLANNER,
            sql_generator_llm=dataclasses.replace(GENERATOR, provider=generator_provider),
        )

    def test_uses_sql_generator_settings_not_planner_settings(self):
        client = build_sql_generator(self._registry(), profile_name=None)
        self.assertIsInstance(client, BedrockClient)
        self.assertEqual(client._settings.model_id, "sql-model")
        self.assertEqual(client._settings.max_output_tokens, 500)

    def test_planner_and_generator_make_separate_calls(self):
        planner_runtime = FakeRuntime(text='{"status": "ready"}')
        generator_runtime = FakeRuntime()
        planner = _client(PLANNER, planner_runtime)
        generator = _client(GENERATOR, generator_runtime)

        planner_text = planner.complete("planner prompt")
        sql_text, stage = generate_sql(generator, "sql prompt")

        self.assertEqual(len(planner_runtime.calls), 1)
        self.assertEqual(len(generator_runtime.calls), 1)
        self.assertEqual(planner_runtime.calls[0]["modelId"], "planner-model")
        self.assertEqual(generator_runtime.calls[0]["modelId"], "sql-model")
        self.assertNotEqual(planner_text, sql_text)
        self.assertEqual(stage.stage, SQL_GENERATE_STAGE)

    def test_non_bedrock_generator_provider_is_rejected(self):
        with self.assertRaises(LlmError):
            build_sql_generator(self._registry(generator_provider="openai"))


if __name__ == "__main__":
    unittest.main()

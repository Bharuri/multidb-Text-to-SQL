import unittest
from unittest.mock import patch

from src.query.config import LlmSettings
from src.query.llm import (
    BedrockClient,
    Credentials,
    LlmError,
    boto_session,
    make_client,
    masked_access_key,
    profile_status,
)

SETTINGS = LlmSettings(
    provider="bedrock",
    model_id="test-model",
    region="us-east-1",
    temperature=0.0,
    max_output_tokens=100,
)


class FakeRuntime:
    def __init__(self, text_parts=("he", "llo"), fail=None):
        self.text_parts = text_parts
        self.fail = fail
        self.calls = []

    def converse(self, **request):
        self.calls.append(("converse", request))
        if self.fail:
            raise self.fail
        return {"output": {"message": {"content": [{"text": "".join(self.text_parts)}]}}}

    def converse_stream(self, **request):
        self.calls.append(("stream", request))
        if self.fail:
            raise self.fail
        events = [{"contentBlockDelta": {"delta": {"text": part}}} for part in self.text_parts]
        events.append({"messageStop": {"stopReason": "end_turn"}})
        return {"stream": iter(events)}


class BedrockClientTests(unittest.TestCase):
    def test_complete_without_callback_uses_converse(self):
        runtime = FakeRuntime()
        client = BedrockClient(SETTINGS, runtime=runtime)
        self.assertEqual(client.complete("hi"), "hello")
        kind, request = runtime.calls[0]
        self.assertEqual(kind, "converse")
        self.assertEqual(request["modelId"], "test-model")
        self.assertEqual(request["inferenceConfig"], {"temperature": 0.0, "maxTokens": 100})
        self.assertEqual(request["messages"][0]["content"][0]["text"], "hi")

    def test_complete_with_callback_streams_chunks(self):
        runtime = FakeRuntime()
        client = BedrockClient(SETTINGS, runtime=runtime)
        chunks = []
        self.assertEqual(client.complete("hi", on_chunk=chunks.append), "hello")
        self.assertEqual(chunks, ["he", "llo"])
        self.assertEqual(runtime.calls[0][0], "stream")

    def test_provider_errors_become_llm_error(self):
        client = BedrockClient(SETTINGS, runtime=FakeRuntime(fail=RuntimeError("throttled")))
        with self.assertRaises(LlmError) as ctx:
            client.complete("hi")
        self.assertEqual(ctx.exception.category, "llm_error")
        self.assertIn("RuntimeError", str(ctx.exception))
        self.assertNotIn("throttled", str(ctx.exception))

    def test_streaming_errors_also_become_llm_error(self):
        client = BedrockClient(SETTINGS, runtime=FakeRuntime(fail=ValueError("bad")))
        with self.assertRaises(LlmError):
            client.complete("hi", on_chunk=lambda _chunk: None)

    def test_non_bedrock_provider_is_rejected(self):
        other = LlmSettings(provider="openai", model_id="m", region="r", temperature=0.0, max_output_tokens=1)
        with self.assertRaisesRegex(LlmError, "unsupported LLM provider"):
            BedrockClient(other, runtime=FakeRuntime())
        with self.assertRaisesRegex(LlmError, "unsupported LLM provider"):
            make_client(other)

    def test_make_client_builds_bedrock_client_without_network(self):
        client = make_client(SETTINGS, profile_name=None)
        self.assertIsInstance(client, BedrockClient)


class CredentialsTests(unittest.TestCase):
    def test_secret_fields_are_hidden_from_repr(self):
        creds = Credentials(access_key_id="AKIDEXAMPLE", secret_access_key="s3cr3t", session_token="tok")
        text = repr(creds)
        self.assertNotIn("s3cr3t", text)
        self.assertNotIn("AKIDEXAMPLE", text)
        self.assertNotIn("tok", text)

    def test_explicit_keys_are_passed_to_boto_session(self):
        creds = Credentials(access_key_id="AKID", secret_access_key="SECRET", session_token="TOKEN")
        with patch("boto3.Session") as session:
            boto_session("us-east-1", profile_name="multidb", credentials=creds)
        session.assert_called_once_with(
            aws_access_key_id="AKID",
            aws_secret_access_key="SECRET",
            aws_session_token="TOKEN",
            region_name="us-east-1",
        )

    def test_without_keys_the_profile_chain_is_used(self):
        with patch("boto3.Session") as session:
            boto_session("us-east-1", profile_name="multidb", credentials=None)
        session.assert_called_once_with(profile_name="multidb", region_name="us-east-1")

    def test_make_client_passes_credentials_through(self):
        creds = Credentials(access_key_id="AKID", secret_access_key="SECRET")
        with patch("boto3.Session") as session:
            make_client(SETTINGS, credentials=creds)
        self.assertEqual(session.call_args.kwargs["aws_access_key_id"], "AKID")
        self.assertEqual(session.call_args.kwargs["aws_secret_access_key"], "SECRET")


class ProfileStatusTests(unittest.TestCase):
    def test_masked_key_keeps_only_edges(self):
        self.assertEqual(masked_access_key("AKIAEXAMPLEKEYID0000"), "AKIA…0000")

    def test_short_key_is_fully_hidden(self):
        self.assertEqual(masked_access_key("AKIA123"), "****")

    def test_status_names_profile_and_masks_key_without_secret(self):
        resolved = type("Resolved", (), {"access_key": "AKIAEXAMPLEKEYID0000", "secret_key": "TOPSECRET"})()
        with patch("boto3.Session") as session, patch.dict("os.environ", {"AWS_PROFILE": "multidb"}):
            session.return_value.get_credentials.return_value = resolved
            text = profile_status()
        self.assertEqual(text, "Using AWS profile 'multidb' (access key ID AKIA…0000).")
        self.assertNotIn("TOPSECRET", text)
        self.assertNotIn("AKIAEXAMPLEKEYID0000", text)

    def test_no_credentials_gives_none(self):
        with patch("boto3.Session") as session:
            session.return_value.get_credentials.return_value = None
            self.assertIsNone(profile_status())

    def test_broken_profile_gives_none(self):
        with patch("boto3.Session", side_effect=RuntimeError("bad profile")):
            self.assertIsNone(profile_status())


if __name__ == "__main__":
    unittest.main()

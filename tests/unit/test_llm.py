import unittest
from unittest import mock

from botocore.exceptions import ClientError, NoCredentialsError

from src.query import pipeline
from src.query.llm import LlmError, complete
from src.query.pipeline import Credentials, Request, final_status, run

SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
ACCESS = "AKIAEXAMPLEACCESSKEY"


def text_event(text: str) -> dict:
    return {"contentBlockDelta": {"delta": {"text": text}, "contentBlockIndex": 0}}


class FakeClient:
    def __init__(self, texts=(), error: Exception | None = None, events=None) -> None:
        self.texts = list(texts)
        self.error = error
        self.events = events
        self.calls: list[dict] = []
        self.log: list[tuple] = []

    def converse_stream(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"stream": self._stream()}

    def _stream(self):
        if self.events is not None:
            for event in self.events:
                yield event
            return
        for index, text in enumerate(self.texts):
            self.log.append(("yield", index))
            yield text_event(text)
        self.log.append(("end", None))


def bad_credentials() -> ClientError:
    return ClientError(
        {"Error": {"Code": "UnrecognizedClientException", "Message": "The security token included in the request is invalid."}},
        "ConverseStream",
    )


class CompleteTests(unittest.TestCase):
    def call(self, client: FakeClient, on_chunk=None) -> str:
        return complete(
            "the prompt",
            model_id="model-x",
            region="us-east-1",
            temperature=0,
            max_output_tokens=50,
            on_chunk=on_chunk,
            client=client,
        )

    def test_returns_the_joined_reply(self) -> None:
        client = FakeClient(["{", '"table": "Orders"', "}"])
        self.assertEqual(self.call(client), '{"table": "Orders"}')

    def test_request_carries_prompt_model_and_inference_settings(self) -> None:
        client = FakeClient(["ok"])
        self.call(client)
        call = client.calls[0]
        self.assertEqual(call["modelId"], "model-x")
        self.assertEqual(call["messages"], [{"role": "user", "content": [{"text": "the prompt"}]}])
        self.assertEqual(call["inferenceConfig"], {"temperature": 0, "maxTokens": 50})

    def test_each_chunk_reaches_the_callback_before_the_stream_ends(self) -> None:
        client = FakeClient(["a", "b", "c"])
        seen: list[tuple] = []
        self.call(client, on_chunk=lambda text: seen.append(("chunk", text, list(client.log))))
        self.assertEqual([item[1] for item in seen], ["a", "b", "c"])
        for index, (_, text, log_at_chunk) in enumerate(seen):
            self.assertIn(("yield", index), log_at_chunk)
            self.assertNotIn(("end", None), log_at_chunk)

    def test_empty_reply_is_an_error(self) -> None:
        with self.assertRaisesRegex(LlmError, "returned no text"):
            self.call(FakeClient(["   "]))

    def test_bad_credentials_give_the_error_code_only(self) -> None:
        with self.assertRaises(LlmError) as raised:
            self.call(FakeClient(error=bad_credentials()))
        self.assertEqual(str(raised.exception), "Bedrock rejected the request (UnrecognizedClientException).")

    def test_stream_error_event_is_an_error(self) -> None:
        events = [text_event("partial"), {"throttlingException": {"message": "slow down"}}]
        with self.assertRaisesRegex(LlmError, "throttlingException"):
            self.call(FakeClient(events=events))

    def test_missing_credentials_message(self) -> None:
        with self.assertRaisesRegex(LlmError, "No AWS credentials found"):
            self.call(FakeClient(error=NoCredentialsError()))

    def test_unexpected_exception_shows_only_its_type(self) -> None:
        with self.assertRaises(LlmError) as raised:
            self.call(FakeClient(error=RuntimeError(SECRET)))
        self.assertEqual(str(raised.exception), "LLM call failed (RuntimeError).")

    def test_secret_never_appears_in_the_error(self) -> None:
        client = FakeClient(error=bad_credentials())
        with self.assertRaises(LlmError) as raised:
            complete(
                "p",
                model_id="m",
                region="us-east-1",
                temperature=0,
                max_output_tokens=10,
                access_key_id=ACCESS,
                secret_access_key=SECRET,
                client=client,
            )
        self.assertNotIn(SECRET, str(raised.exception))
        self.assertNotIn(ACCESS, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)


class PipelineLlmTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch("src.query.pipeline._attach_log_file")
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, credentials: Credentials | None = None) -> Request:
        return Request(database="sample_superstore", question="how many phones", credentials=credentials)

    def test_bad_credentials_end_in_error_and_keys_appear_nowhere(self) -> None:
        creds = Credentials(access_key_id=ACCESS, secret_access_key=SECRET)
        client = FakeClient(error=bad_credentials())
        with mock.patch("src.query.llm._bedrock_client", return_value=client):
            trace = run(self.request(creds), on_stage_done=lambda result: None)
        self.assertEqual(final_status(trace), "error")
        llm_stage = next(result for result in trace if result.stage == "llm")
        self.assertEqual(llm_stage.status, "failed")
        self.assertEqual(next(result for result in trace if result.stage == "plan").status, "skipped")
        dump = repr(trace)
        self.assertNotIn(SECRET, dump)
        self.assertNotIn(ACCESS, dump)

    def test_keys_are_passed_to_the_client_factory(self) -> None:
        creds = Credentials(access_key_id=ACCESS, secret_access_key=SECRET, session_token="tok")
        client = FakeClient(["{}"])
        with mock.patch("src.query.llm._bedrock_client", return_value=client) as factory:
            pipeline.stage_llm(self.request(creds), [pipeline._ok("prompt", {"prompt": "p"})])
        factory.assert_called_once_with("us-east-1", ACCESS, SECRET, "tok")

    def test_no_keys_use_the_default_chain(self) -> None:
        with mock.patch("src.query.llm._bedrock_client", return_value=FakeClient(["{}"])) as factory:
            pipeline.stage_llm(self.request(), [pipeline._ok("prompt", {"prompt": "p"})])
        factory.assert_called_once_with("us-east-1", None, None, None)

    def test_oversized_prompt_never_calls_the_llm(self) -> None:
        small_limit = {"default_row_limit": 1000, "max_prompt_chars": 100, "value_list_threshold": 30}
        with mock.patch("src.query.pipeline._retrieval_config", return_value=small_limit), mock.patch(
            "src.query.pipeline.complete"
        ) as llm_call:
            trace = run(self.request(), on_stage_done=lambda result: None)
        llm_call.assert_not_called()
        prompt_stage = next(result for result in trace if result.stage == "prompt")
        self.assertEqual(prompt_stage.status, "failed")
        self.assertEqual(prompt_stage.output["limit"], 100)
        self.assertEqual(final_status(trace), "error")
        self.assertEqual(next(result for result in trace if result.stage == "llm").status, "skipped")

    def test_llm_reply_is_stored_on_the_stage(self) -> None:
        with mock.patch("src.query.pipeline.complete", return_value='{"table": "Orders"}') as llm_call:
            result = pipeline.stage_llm(self.request(), [pipeline._ok("prompt", {"prompt": "p"})])
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.output["raw_reply"], '{"table": "Orders"}')
        self.assertEqual(llm_call.call_args.args[0], "p")


if __name__ == "__main__":
    unittest.main()

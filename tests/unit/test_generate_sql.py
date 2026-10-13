import unittest

from botocore.exceptions import ClientError

from src.query.llm import LlmError, SqlReply, SqlReplyError, generate_sql


def text_event(text: str) -> dict:
    return {"contentBlockDelta": {"delta": {"text": text}, "contentBlockIndex": 0}}


class FakeClient:
    def __init__(self, texts=(), error: Exception | None = None) -> None:
        self.texts = list(texts)
        self.error = error
        self.calls: list[dict] = []
        self.log: list[tuple] = []

    def converse_stream(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"stream": self._stream()}

    def _stream(self):
        for index, text in enumerate(self.texts):
            self.log.append(("yield", index))
            yield text_event(text)
        self.log.append(("end", None))


def call(client: FakeClient, on_chunk=None) -> SqlReply:
    return generate_sql(
        "the sql prompt",
        model_id="sql-model",
        region="us-east-1",
        temperature=0,
        max_output_tokens=80,
        on_chunk=on_chunk,
        client=client,
    )


GOOD = '{"sql": "SELECT COUNT(*) AS n FROM \\"Orders\\" WHERE \\"Sub_Category\\" = ?", "params": ["Phones"]}'


class GenerateSqlTests(unittest.TestCase):
    def test_returns_raw_sql_and_params(self) -> None:
        reply = call(FakeClient([GOOD]))
        self.assertEqual(reply.raw, GOOD)
        self.assertTrue(reply.sql.startswith("SELECT COUNT(*)"))
        self.assertEqual(reply.params, ["Phones"])

    def test_fenced_reply_is_accepted(self) -> None:
        reply = call(FakeClient(["```json\n", GOOD, "\n```"]))
        self.assertEqual(reply.params, ["Phones"])

    def test_request_uses_the_given_model_and_prompt(self) -> None:
        client = FakeClient([GOOD])
        call(client)
        self.assertEqual(client.calls[0]["modelId"], "sql-model")
        self.assertEqual(client.calls[0]["messages"], [{"role": "user", "content": [{"text": "the sql prompt"}]}])

    def test_chunks_reach_the_callback_before_the_stream_ends(self) -> None:
        client = FakeClient([GOOD[:20], GOOD[20:]])
        seen: list[tuple] = []
        call(client, on_chunk=lambda text: seen.append((text, list(client.log))))
        self.assertEqual("".join(text for text, _ in seen), GOOD)
        self.assertIn(("yield", 0), seen[0][1])
        self.assertNotIn(("end", None), seen[0][1])

    def test_malformed_reply_raises_and_keeps_the_raw_text(self) -> None:
        bad = "SELECT 1"
        with self.assertRaises(SqlReplyError) as caught:
            call(FakeClient([bad]))
        self.assertEqual(caught.exception.raw, bad)
        self.assertTrue(caught.exception.reasons)

    def test_wrong_keys_are_malformed(self) -> None:
        with self.assertRaises(SqlReplyError):
            call(FakeClient(['{"sql": "SELECT 1", "params": [], "extra": 1}']))

    def test_params_must_be_a_list(self) -> None:
        with self.assertRaises(SqlReplyError):
            call(FakeClient(['{"sql": "SELECT 1", "params": "x"}']))

    def test_malformed_reply_is_not_retried(self) -> None:
        client = FakeClient(["not json"])
        with self.assertRaises(SqlReplyError):
            call(client)
        self.assertEqual(len(client.calls), 1)

    def test_transport_errors_pass_through_as_llm_error(self) -> None:
        error = ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "ConverseStream")
        with self.assertRaises(LlmError) as caught:
            call(FakeClient(error=error))
        self.assertNotIsInstance(caught.exception, SqlReplyError)
        self.assertIn("ThrottlingException", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

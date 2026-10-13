"""Stage 4 (llm). Streams one Bedrock ConverseStream call and returns the whole reply.

Credentials are never put in an error message. Errors carry the AWS error code or the exception type only.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError, PartialCredentialsError


class LlmError(Exception):
    """The LLM call failed. The message is safe to show to the user."""


def complete(
    prompt: str,
    *,
    model_id: str,
    region: str,
    temperature: float,
    max_output_tokens: int,
    access_key_id: str | None = None,
    secret_access_key: str | None = None,
    session_token: str | None = None,
    on_chunk: Callable[[str], None] | None = None,
    client: Any = None,
) -> str:
    """Send the prompt and return the full reply. Each text chunk goes to on_chunk as it arrives.

    Pass client only in tests. Without it, a bedrock-runtime client is built from the supplied keys,
    or from the default AWS credential chain when no keys are given.
    """
    try:
        if client is None:
            client = _bedrock_client(region, access_key_id, secret_access_key, session_token)
        response = client.converse_stream(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"temperature": temperature, "maxTokens": max_output_tokens},
        )
        chunks: list[str] = []
        for event in response["stream"]:
            text = _text_of(event)
            if text:
                chunks.append(text)
                if on_chunk is not None:
                    on_chunk(text)
    except LlmError:
        raise
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "Unknown")
        raise LlmError(f"Bedrock rejected the request ({code}).") from None
    except (NoCredentialsError, PartialCredentialsError):
        raise LlmError("No AWS credentials found. Enter keys in the sidebar or configure the AWS credential chain.") from None
    except BotoCoreError as error:
        raise LlmError(f"Could not reach Bedrock in {region} ({type(error).__name__}).") from None
    except Exception as error:
        raise LlmError(f"LLM call failed ({type(error).__name__}).") from None

    reply = "".join(chunks)
    if not reply.strip():
        raise LlmError("The LLM returned no text.")
    return reply


def _text_of(event: dict[str, Any]) -> str:
    """Return the text of a content delta. A stream error event raises LlmError with its code only."""
    error_codes = [key for key in event if key.endswith("Exception")]
    if error_codes:
        raise LlmError(f"Bedrock stream error ({error_codes[0]}).")
    delta = event.get("contentBlockDelta", {}).get("delta", {})
    return delta.get("text", "")


def _bedrock_client(region: str, access_key_id: str | None, secret_access_key: str | None, session_token: str | None) -> Any:
    if access_key_id and secret_access_key:
        session = boto3.Session(
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            aws_session_token=session_token,
            region_name=region,
        )
    else:
        session = boto3.Session(region_name=region)
    return session.client("bedrock-runtime")

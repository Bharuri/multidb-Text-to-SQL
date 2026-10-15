"""LLM provider abstraction (C3).

The pipeline depends only on `LlmClient.complete`. The Bedrock adapter streams through
`converse_stream` when the caller passes a chunk callback, and falls back to `converse`
otherwise. Any provider failure is raised as `LlmError`, which the pipeline reports as
the `llm_error` category.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from src.query.config import LlmSettings


class LlmError(Exception):
    category = "llm_error"


@dataclass(frozen=True)
class Credentials:
    """Optional static AWS keys. Secret fields are excluded from repr so they never reach logs."""

    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    session_token: str | None = field(default=None, repr=False)


def masked_access_key(key: str) -> str:
    """First four and last four characters only; the full ID is never shown."""
    return f"{key[:4]}…{key[-4:]}" if len(key) > 8 else "****"


def profile_status() -> str | None:
    """Describe the profile chain's access key ID without reading the secret."""
    import boto3

    try:
        resolved = boto3.Session().get_credentials()
    except Exception:  # noqa: BLE001 - missing or broken profile; the form still works
        return None
    if resolved is None or not resolved.access_key:
        return None
    profile = os.environ.get("AWS_PROFILE", "default")
    return f"Using AWS profile '{profile}' (access key ID {masked_access_key(resolved.access_key)})."


def boto_session(region: str, profile_name: str | None = None, credentials: Credentials | None = None) -> Any:
    """Explicit keys win; otherwise the AWS profile chain is used."""
    import boto3

    if credentials is not None:
        return boto3.Session(
            aws_access_key_id=credentials.access_key_id,
            aws_secret_access_key=credentials.secret_access_key,
            aws_session_token=credentials.session_token,
            region_name=region,
        )
    return boto3.Session(profile_name=profile_name, region_name=region)


class LlmClient(Protocol):
    def complete(self, prompt: str, on_chunk: Callable[[str], None] | None = None) -> str: ...


class BedrockClient:
    def __init__(
        self,
        settings: LlmSettings,
        profile_name: str | None = None,
        runtime: Any = None,
        credentials: Credentials | None = None,
    ):
        if settings.provider != "bedrock":
            raise LlmError(f"unsupported LLM provider: {settings.provider}")
        self._settings = settings
        if runtime is None:
            try:
                session = boto_session(settings.region, profile_name, credentials)
                runtime = session.client("bedrock-runtime")
            except Exception as exc:  # credentials, region, or botocore configuration problems
                raise LlmError(f"could not create Bedrock client: {type(exc).__name__}") from exc
        self._runtime = runtime

    def complete(self, prompt: str, on_chunk: Callable[[str], None] | None = None) -> str:
        request = {
            "modelId": self._settings.model_id,
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
            "inferenceConfig": {
                "temperature": self._settings.temperature,
                "maxTokens": self._settings.max_output_tokens,
            },
        }
        try:
            if on_chunk is None:
                response = self._runtime.converse(**request)
                blocks = response["output"]["message"]["content"]
                return "".join(block.get("text", "") for block in blocks)
            return self._stream(request, on_chunk)
        except LlmError:
            raise
        except Exception as exc:
            raise LlmError(f"Bedrock call failed: {type(exc).__name__}") from exc

    def _stream(self, request: dict[str, Any], on_chunk: Callable[[str], None]) -> str:
        response = self._runtime.converse_stream(**request)
        parts: list[str] = []
        for event in response["stream"]:
            delta = event.get("contentBlockDelta", {}).get("delta", {})
            text = delta.get("text")
            if text:
                parts.append(text)
                on_chunk(text)
        return "".join(parts)


def make_client(
    settings: LlmSettings,
    profile_name: str | None = None,
    credentials: Credentials | None = None,
) -> LlmClient:
    if settings.provider != "bedrock":
        raise LlmError(f"unsupported LLM provider: {settings.provider}")
    return BedrockClient(settings, profile_name=profile_name, credentials=credentials)

"""Pluggable chat-model transport used by planning and final answers."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Iterator, Protocol

import requests

from .config import Settings
from .errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class CompletionResult:
    content: str
    status_code: int


class LLMTransportError(RuntimeError):
    def __init__(self, cause: Exception) -> None:
        self.cause_type = type(cause).__name__
        super().__init__(f"{self.cause_type}: {cause}")


class LLMResponseError(RuntimeError):
    def __init__(self, cause: Exception | str) -> None:
        if isinstance(cause, Exception):
            self.cause_type = type(cause).__name__
            message = f"{self.cause_type}: {cause}"
        else:
            self.cause_type = "InvalidResponse"
            message = cause
        super().__init__(message)


class ChatModelClient(Protocol):
    @property
    def available(self) -> bool: ...

    def complete_json(
        self,
        *,
        system_prompt: str,
        payload: dict[str, object],
    ) -> CompletionResult: ...

    def stream_text(
        self,
        *,
        system_prompt: str,
        payload: dict[str, object],
    ) -> Iterator[str]: ...


class OpenAICompatibleClient:
    """OpenAI-compatible Chat Completions implementation."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def available(self) -> bool:
        return bool(
            self.settings.llm_chat_completions_url
            and self.settings.llm_api_key
        )

    def complete_json(
        self,
        *,
        system_prompt: str,
        payload: dict[str, object],
    ) -> CompletionResult:
        try:
            response = requests.post(
                self.settings.llm_chat_completions_url,
                headers=self._headers(),
                json={
                    "model": self.settings.llm_model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {
                            "role": "user",
                            "content": json.dumps(payload, ensure_ascii=False),
                        },
                    ],
                    "temperature": 0,
                    "stream": False,
                    "enable_thinking": False,
                    "response_format": {"type": "json_object"},
                },
                timeout=self.settings.llm_timeout_seconds,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        except requests.RequestException as exc:
            raise LLMTransportError(exc) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise LLMResponseError(exc) from exc
        return CompletionResult(str(content), response.status_code)

    def stream_text(
        self,
        *,
        system_prompt: str,
        payload: dict[str, object],
    ) -> Iterator[str]:
        try:
            with requests.post(
                self.settings.llm_chat_completions_url,
                headers=self._headers(),
                json={
                    "model": self.settings.llm_model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {
                            "role": "user",
                            "content": json.dumps(
                                payload,
                                ensure_ascii=False,
                                default=str,
                            ),
                        },
                    ],
                    "temperature": 0,
                    "stream": True,
                    "enable_thinking": False,
                },
                timeout=self.settings.llm_timeout_seconds,
                stream=True,
            ) as response:
                response.raise_for_status()
                response.encoding = "utf-8"
                yielded = False
                for line in response.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                        choices = event["choices"]
                        if choices == []:
                            continue
                        content = choices[0]["delta"].get("content")
                    except (json.JSONDecodeError, KeyError, TypeError, IndexError) as exc:
                        raise LLMResponseError(exc) from exc
                    if isinstance(content, str) and content:
                        yielded = True
                        yield content
                if not yielded:
                    raise LLMResponseError("LLM未返回有效的流式内容")
        except requests.RequestException as exc:
            raise LLMTransportError(exc) from exc

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.llm_api_key}",
            "Content-Type": "application/json",
        }


def create_llm_client(settings: Settings) -> ChatModelClient:
    if settings.llm_provider == "openai_compatible":
        return OpenAICompatibleClient(settings)
    raise ConfigurationError(f"不支持的LLM供应商：{settings.llm_provider}")

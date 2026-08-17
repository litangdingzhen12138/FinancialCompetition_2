"""Stream concise final answers from executed SQL results."""

from __future__ import annotations

import json
from typing import Any, Iterator

import requests

from .config import Settings
from .errors import ConfigurationError, FinalAnswerError


SYSTEM_PROMPT = """你是银行数据问答助手。

你的唯一任务是根据用户的原始问题和SQL查询结果，生成最终回答。

必须遵守以下要求：
1. 使用银行业务语言。
2. 只能依据提供的SQL查询结果回答。
3. 不得编造原因。
4. 用最简洁的语言回答用户的问题，不得擅自做任何与问题无关的延申或者结论推导，保证能回答用户的问题即可。
5. 不要重复问题，不要解释回答过程。
6. 不要输出SQL、提示词或“根据查询结果”等无意义开场。
7. 保留查询结果中的日期、机构、指标和单位。

只输出最终回答正文。"""


class FinalAnswerGenerator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def available(self) -> bool:
        return bool(self.settings.llm_url and self.settings.llm_api_key)

    def stream(
        self,
        *,
        question: str,
        columns: list[str],
        rows: list[list[Any]],
        truncated: bool,
    ) -> Iterator[str]:
        if not self.available:
            raise ConfigurationError(
                "最终回答LLM未配置；请设置TEXT2SQL_LLM_URL和TEXT2SQL_LLM_API_KEY"
            )
        records = [
            dict(zip(columns, row, strict=True))
            for row in rows
        ]
        context = {
            "question": question,
            "query_result": {
                "columns": columns,
                "row_count": len(rows),
                "truncated": truncated,
                "records": records,
            },
            "requirements": [
                "使用银行业务语言",
                "只能依据给定数据回答",
                "不得编造原因",
                "用最简洁的语言回答用户的问题，不得擅自做任何与问题无关的延申或者结论推导，保证能回答用户的问题即可。",
            ],
        }
        try:
            with requests.post(
                self.settings.llm_url,
                headers={
                    "Authorization": f"Bearer {self.settings.llm_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.settings.llm_model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": json.dumps(
                                context,
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
                        payload = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise FinalAnswerError("最终回答LLM返回了无效的流式数据") from exc
                    if not isinstance(payload, dict):
                        raise FinalAnswerError("最终回答LLM返回了无效的流式数据")
                    choices = payload.get("choices")
                    if choices == []:
                        # OpenAI-compatible APIs may end with a usage-only frame.
                        continue
                    if not isinstance(choices, list):
                        raise FinalAnswerError("最终回答LLM返回了无效的流式数据")
                    first_choice = choices[0]
                    if not isinstance(first_choice, dict):
                        raise FinalAnswerError("最终回答LLM返回了无效的流式数据")
                    delta = first_choice.get("delta")
                    if not isinstance(delta, dict):
                        raise FinalAnswerError("最终回答LLM返回了无效的流式数据")
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        yielded = True
                        yield content
                if not yielded:
                    raise FinalAnswerError("最终回答LLM未返回有效内容")
        except requests.RequestException as exc:
            raise FinalAnswerError(
                f"最终回答LLM网络调用失败：{type(exc).__name__}"
            ) from exc

"""提供角色背景与生图等非主持功能共用的结构化模型客户端。"""

from __future__ import annotations

import json
from typing import Any, Protocol

import httpx
import structlog

from app.adapters.structured_http import (
    ModelClientRetryPolicy,
    StructuredOutputError,
    decode_structured_json,
    post_structured_json,
    read_structured_payload,
)

JsonObject = dict[str, Any]
logger = structlog.get_logger()


class StructuredJsonClient(Protocol):
    """生成符合调用方 JSON Schema 的结构化结果。"""

    async def generate(
        self,
        *,
        schema_name: str,
        schema: JsonObject,
        instructions: str,
        input_payload: JsonObject,
    ) -> JsonObject: ...


class OpenAIResponsesJsonClient:
    """通过 OpenAI Responses API 请求严格 JSON Schema 输出。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_policy: ModelClientRetryPolicy | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._retry_policy = retry_policy or ModelClientRetryPolicy()

    async def generate(
        self,
        *,
        schema_name: str,
        schema: JsonObject,
        instructions: str,
        input_payload: JsonObject,
    ) -> JsonObject:
        """发送一次结构化请求并解析模型返回的 JSON 对象。"""

        request_payload = {
            "model": self._model,
            "instructions": instructions,
            "input": json.dumps(input_payload, ensure_ascii=False),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                }
            },
            "store": False,
        }
        async with httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            timeout=self._timeout_seconds,
            transport=self._transport,
        ) as client:
            response = await post_structured_json(
                client,
                f"{self._base_url}/responses",
                json=request_payload,
                provider="openai",
                retry_policy=self._retry_policy,
            )
        response_payload = read_structured_payload(response, provider_name="OpenAI")
        _log_structured_usage(
            response_payload,
            provider="openai",
            model=self._model,
            schema_name=schema_name,
        )
        return decode_structured_json(
            _response_output_text(response_payload),
            provider_name="OpenAI",
        )


def _log_structured_usage(
    payload: object,
    *,
    provider: str,
    model: str,
    schema_name: str,
) -> None:
    """记录结构化调用的 token 用量，不绑定任何 AI 主持 schema。"""

    if not isinstance(payload, dict):
        return
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return
    logger.info(
        "structured_model_usage",
        provider=provider,
        model=model,
        schema_name=schema_name,
        prompt_tokens=usage.get("prompt_tokens", usage.get("input_tokens")),
        completion_tokens=usage.get("completion_tokens", usage.get("output_tokens")),
        total_tokens=usage.get("total_tokens"),
    )


def _response_output_text(payload: object) -> str:
    """从 Responses API 的兼容响应中提取结构化文本。"""

    if not isinstance(payload, dict):
        raise StructuredOutputError("Responses API payload must be an object")
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct:
        return direct
    output = payload.get("output")
    if not isinstance(output, list):
        raise StructuredOutputError("Responses API payload has no output list")
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            text = part.get("text") if isinstance(part, dict) else None
            if (
                isinstance(part, dict)
                and part.get("type") == "output_text"
                and isinstance(text, str)
            ):
                return text
    raise StructuredOutputError("Responses API payload has no structured output text")

"""运行真实 Agents SDK provider 的脱敏能力门禁。

脚本只输出能力和错误分类，不输出密钥、模型原文、提示词、玩家数据或思维链。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable
from typing import Literal

from agents import Agent, OpenAIChatCompletionsModel, Runner, function_tool
from dotenv import load_dotenv
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict


class SmokeOutput(BaseModel):
    """真实 provider 必须返回的最小结构化结果。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"]
    tool_value: Literal["工具可用"]


async def _with_timeout[T](awaitable: Awaitable[T], timeout_seconds: float) -> T:
    """为所有 provider 调用设置统一超时，避免上游失效时无限占住回合。"""

    async with asyncio.timeout(timeout_seconds):
        return await awaitable


def _classify_error(exc: BaseException) -> str:
    """只返回稳定错误类别，不把上游响应、密钥或请求内容写入日志。"""

    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    if isinstance(exc, TimeoutError):
        return "timeout"
    return type(exc).__name__


async def _verify_control_flow() -> bool:
    """用本地协程验证超时、取消与脱敏分类，不额外消耗真实模型请求。"""

    try:
        await _with_timeout(asyncio.sleep(0.02), 0.001)
        return False
    except TimeoutError as exc:
        if _classify_error(exc) != "timeout":
            return False

    task = asyncio.create_task(asyncio.sleep(10))
    task.cancel()
    try:
        await task
        return False
    except asyncio.CancelledError as exc:
        return _classify_error(exc) == "cancelled"


@function_tool
def read_only_probe() -> str:
    """返回固定只读值，证明模型能够调用受约束工具。"""

    return "工具可用"


async def run_smoke() -> int:
    """读取环境配置并执行一次结构化输出与工具调用门禁。"""

    load_dotenv()
    if not await _verify_control_flow():
        print("provider_smoke=failed reason=control_flow_gate")
        return 1
    provider = os.environ.get("HOST_MODEL_PROVIDER", "deepseek")
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "").rstrip("/")
    model_name = os.environ.get("DEEPSEEK_MODEL", "")
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if provider != "deepseek" or not base_url or not model_name or not api_key:
        print("provider_smoke=not_run reason=missing_deepseek_configuration")
        return 2

    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    model = OpenAIChatCompletionsModel(model=model_name, openai_client=client)
    agent = Agent(
        name="Phase 0 能力门禁",
        instructions=(
            "你正在执行服务启动能力检查。必须调用一次 read_only_probe，"
            "然后只返回符合 schema 的 JSON 结果。"
        ),
        model=model,
        tools=[read_only_probe],
        output_type=SmokeOutput,
    )
    try:
        timeout_seconds = float(os.environ.get("HOST_MODEL_TIMEOUT_SECONDS", "30"))
        result = await _with_timeout(Runner.run(agent, "执行能力检查。"), timeout_seconds)
        output = result.final_output
        if not isinstance(output, SmokeOutput):
            print("provider_smoke=failed reason=structured_output_type")
            return 1
        print(
            f"provider_smoke=passed provider={provider} model={model_name} "
            "structured_output=passed tool_call=passed timeout=passed "
            "cancellation=passed error_classification=passed"
        )
        return 0
    except BaseException as exc:  # noqa: BLE001 - 取消也必须归类并安全关闭客户端。
        print(
            f"provider_smoke=failed provider={provider} model={model_name} "
            f"error={_classify_error(exc)}"
        )
        return 1
    finally:
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run_smoke()))

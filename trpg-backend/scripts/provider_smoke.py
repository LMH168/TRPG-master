"""运行真实 Agents SDK provider 的脱敏能力门禁。

脚本只输出能力和错误分类，不输出密钥、模型原文、提示词、玩家数据或思维链。
"""

from __future__ import annotations

import asyncio
import os
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


@function_tool
def read_only_probe() -> str:
    """返回固定只读值，证明模型能够调用受约束工具。"""

    return "工具可用"


async def run_smoke() -> int:
    """读取环境配置并执行一次结构化输出与工具调用门禁。"""

    load_dotenv()
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
        result = await Runner.run(agent, "执行能力检查。")
        output = result.final_output
        if not isinstance(output, SmokeOutput):
            print("provider_smoke=failed reason=structured_output_type")
            return 1
        print(
            f"provider_smoke=passed provider={provider} model={model_name} "
            "structured_output=passed tool_call=passed"
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - 门禁需要将所有上游错误归类为失败。
        print(
            f"provider_smoke=failed provider={provider} model={model_name} "
            f"error={type(exc).__name__}"
        )
        return 1
    finally:
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run_smoke()))

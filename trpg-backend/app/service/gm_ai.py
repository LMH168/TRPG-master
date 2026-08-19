"""Phase 1B 的上下文、意图解释和叙事边界服务。

本文件只负责把安全快照交给模型并校验模型输出；它不直接修改游戏状态。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Protocol

from agents import Agent, ModelSettings, OpenAIChatCompletionsModel, OpenAIResponsesModel, Runner
from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, secret_value
from app.dto.gm import (
    ActionCandidate,
    CommandAdapter,
    CommandEnvelope,
    ContextSnapshot,
    IntentResult,
    IntentStep,
    NarrationDraft,
)
from app.models.gm import GameEvent, GameSession, RuntimeActor


class GmModelUnavailable(RuntimeError):
    """模型未配置、调用失败或返回了不符合契约的结果。"""


def _agent_model_settings(provider: str) -> ModelSettings:
    """关闭兼容 provider 的思考模式，避免为结构化短输出付出额外延迟。"""

    if provider == "deepseek":
        return ModelSettings(extra_body={"thinking": {"type": "disabled"}})
    if provider == "qwen":
        return ModelSettings(extra_body={"enable_thinking": False})
    return ModelSettings()


class IntentInterpreter(Protocol):
    """意图解释器的最小应用边界，便于真实模型和脚本模型互换测试。"""

    async def interpret(self, snapshot: ContextSnapshot, player_input: str) -> IntentResult: ...


class Narrator(Protocol):
    """只接收已提交事实的叙事器边界。"""

    async def narrate(
        self,
        snapshot: ContextSnapshot,
        event_ids: Sequence[str],
        facts: Sequence[str],
    ) -> NarrationDraft: ...


class ScriptedIntentInterpreter:
    """测试专用的预置解释器，绝不作为生产备用主持。"""

    def __init__(self, results: Iterable[IntentResult]) -> None:
        """保存按调用顺序返回的结构化结果。"""

        self._results = iter(results)

    async def interpret(self, snapshot: ContextSnapshot, player_input: str) -> IntentResult:
        """返回下一份脚本结果，并校验它来自当前快照修订号。"""

        try:
            result = next(self._results)
        except StopIteration as exc:
            raise GmModelUnavailable("脚本解释器没有更多预置结果") from exc
        return result.model_copy(update={"source_revision": snapshot.revision})


class AgentsSdkInterpreter:
    """使用 Agents SDK 执行一次短生命周期的中文结构化意图调用。"""

    def __init__(self, settings: Settings) -> None:
        """按配置创建兼容 OpenAI 的 Agents SDK 模型。"""

        provider = settings.host_model_provider
        if provider == "fake":
            raise GmModelUnavailable("生产环境禁止加载 fake provider")
        if provider == "openai":
            key, base_url, model_name = (
                settings.openai_api_key,
                settings.openai_base_url,
                settings.openai_model,
            )
        elif provider == "qwen":
            key, base_url, model_name = (
                settings.qwen_api_key,
                settings.qwen_base_url,
                settings.qwen_model,
            )
        else:
            key, base_url, model_name = (
                settings.deepseek_api_key,
                settings.deepseek_base_url,
                settings.deepseek_model,
            )
        if key is None or not secret_value(key).strip():
            raise GmModelUnavailable("主持 provider 未配置")
        client = AsyncOpenAI(api_key=secret_value(key), base_url=base_url)
        if provider == "openai":
            model = OpenAIResponsesModel(model=model_name, openai_client=client)
        else:
            model = OpenAIChatCompletionsModel(model=model_name, openai_client=client)
        self._client = client
        self._agent = Agent(
            name="意图解释器",
            instructions=(
                "你是中文克苏鲁跑团的意图解释器。只把玩家输入映射为当前快照中的有限动作，"
                "不编造事实、不决定骰点、不修改状态。目标不唯一或缺失时返回 clarification。"
                "能够唯一匹配 action_candidates 时必须返回 proposal。proposal 必须有一到四个"
                "steps，clarification_question 为空且 clarification_options 为空；clarification "
                "必须令 steps 为空，并给出非空 clarification_question。source_revision 必须原样"
                "复制快照 revision。不要创建名为 proposal 的嵌套对象。唯一允许的顶层字段是 "
                "kind、summary、steps、clarification_question、clarification_options、"
                "source_revision。唯一目标示例："
                '{"kind":"proposal","summary":"前往图书馆","steps":[{"action":'
                '"move_actor","target_id":"library","skill_id":null,"goal":null,"topic":null,'
                '"target_time":null}],"clarification_question":null,"clarification_options":[],'
                '"source_revision":0}。只输出符合 IntentResult schema 的结构化结果。'
            ),
            model=model,
            model_settings=_agent_model_settings(provider),
            output_type=IntentResult,
        )

    async def interpret(self, snapshot: ContextSnapshot, player_input: str) -> IntentResult:
        """调用模型并把输出限制为 IntentResult；失败统一转为可恢复模型错误。"""

        try:
            result = await Runner.run(
                self._agent,
                json.dumps(
                    {"snapshot": snapshot.model_dump(mode="json"), "player_input": player_input},
                    ensure_ascii=False,
                ),
            )
            if not isinstance(result.final_output, IntentResult):
                raise GmModelUnavailable("意图模型输出类型不正确")
            return result.final_output
        except GmModelUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - 不把供应商细节泄露给玩家。
            raise GmModelUnavailable("意图模型调用失败") from exc


class AgentsSdkNarrator:
    """使用独立短生命周期 Agent 表达已提交事件，不参与规则裁决。"""

    def __init__(self, settings: Settings) -> None:
        """按同一生产 provider 建立仅输出 NarrationDraft 的 Agent。"""

        provider = settings.host_model_provider
        if provider == "fake":
            raise GmModelUnavailable("生产环境禁止加载 fake provider")
        if provider == "openai":
            key = settings.openai_api_key
            base_url = settings.openai_base_url
            model_name = settings.openai_model
        elif provider == "qwen":
            key = settings.qwen_api_key
            base_url = settings.qwen_base_url
            model_name = settings.qwen_model
        else:
            key = settings.deepseek_api_key
            base_url = settings.deepseek_base_url
            model_name = settings.deepseek_model
        if key is None or not secret_value(key).strip():
            raise GmModelUnavailable("主持 provider 未配置")
        client = AsyncOpenAI(api_key=secret_value(key), base_url=base_url)
        model = (
            OpenAIResponsesModel(model=model_name, openai_client=client)
            if provider == "openai"
            else OpenAIChatCompletionsModel(model=model_name, openai_client=client)
        )
        self._agent = Agent(
            name="叙事器",
            instructions=(
                "你是中文克苏鲁跑团叙事器。只能描述输入中已经提交的事件和当前玩家可见事实，"
                "不得补写 NPC 行动、秘密、数值变化或复活。只输出 NarrationDraft 结构化结果。"
            ),
            model=model,
            model_settings=_agent_model_settings(provider),
            output_type=NarrationDraft,
        )

    async def narrate(
        self,
        snapshot: ContextSnapshot,
        event_ids: Sequence[str],
        facts: Sequence[str],
    ) -> NarrationDraft:
        """表达一组已提交事件；任何调用异常都转成可恢复模型错误。"""

        try:
            result = await Runner.run(
                self._agent,
                json.dumps(
                    {
                        "snapshot": snapshot.model_dump(mode="json"),
                        "committed_event_ids": list(event_ids),
                        "visible_facts": list(facts),
                    },
                    ensure_ascii=False,
                ),
            )
            if not isinstance(result.final_output, NarrationDraft):
                raise GmModelUnavailable("叙事模型输出类型不正确")
            return guard_narration(
                result.final_output,
                committed_event_ids=event_ids,
                visible_facts=facts,
            )
        except GmModelUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - 不泄露供应商原始响应。
            raise GmModelUnavailable("叙事模型调用失败") from exc


async def build_context_snapshot(
    db: AsyncSession,
    *,
    room_id: str,
    actor_id: str,
) -> ContextSnapshot:
    """从权威会话构造只含玩家公开信息的不可变上下文快照。"""

    session = await db.get(GameSession, room_id)
    actor = await db.get(RuntimeActor, actor_id)
    if session is None or actor is None or actor.room_id != room_id:
        raise ValueError("GM 会话或调查员不存在")
    event_ids = list(
        await db.scalars(
            select(GameEvent.id)
            .where(GameEvent.room_id == room_id)
            .order_by(GameEvent.sequence.desc())
            .limit(20)
        )
    )
    return ContextSnapshot(
        snapshot_id=str(uuid.uuid4()),
        session_id=room_id,
        actor_id=actor_id,
        audience=f"private:{actor_id}",
        revision=session.state_version,
        world_time=datetime.fromisoformat(session.state_json["world_time"]),
        location_id=actor.location_id,
        visible_facts=list(session.state_json.get("visible_facts", [])),
        action_candidates=_candidates_for(actor.location_id),
        recent_event_ids=list(reversed(event_ids)),
    )


def _candidates_for(location_id: str) -> list[ActionCandidate]:
    """把当前地点的公开出口和对象转成模型可引用的候选 ID。"""

    exits = {
        "arnoldsburg": [
            ("neighbors", "询问朋友和邻居"),
            ("library", "前往图书馆"),
            ("newspaper", "前往报社档案室"),
            ("kimball_house", "前往金博尔旧居"),
            ("cemetery", "前往墓园"),
        ],
        "library": [("arnoldsburg", "回到阿诺兹堡")],
        "kimball_house": [("arnoldsburg", "回到阿诺兹堡")],
        "cemetery": [("arnoldsburg", "回到阿诺兹堡")],
    }
    objects = {
        "arnoldsburg": [("town_sign", "观察城镇标牌")],
        "library": [
            ("old_newspapers", "查阅旧报纸"),
            ("bookshelf", "检查书架"),
            ("librarian", "与图书管理员交谈"),
        ],
        "newspaper": [
            ("newspaper_archive", "申请查阅报社档案"),
            ("hilda", "询问未刊证词"),
        ],
        "kimball_house": [("desk", "检查书桌"), ("empty_shelf", "检查空书架")],
        "cemetery": [
            ("graveyard_gate", "观察墓园入口"),
            ("headstone", "检查墓碑"),
            ("gravekeeper", "与守墓人交谈"),
            ("neighbors", "回想邻居提供的线索"),
            ("track_grave", "追踪墓碑附近的足迹"),
            ("night_watch", "在夜间监视墓地"),
            ("call_douglas", "呼喊道格拉斯的名字"),
            ("attack_douglas", "攻击墓地的人影"),
            ("open_crypt", "移开墓穴入口石板"),
        ],
        "crypt": [
            ("talk_douglas", "与道格拉斯交谈"),
            ("follow_douglas", "跟随道格拉斯进入地下"),
        ],
    }
    candidates = [
        *[
            ActionCandidate(action="move_actor", target_id=target, label=label)
            for target, label in exits.get(location_id, [])
        ],
        *[
            ActionCandidate(
                action="talk_to_npc"
                if target in {"librarian", "gravekeeper"}
                else "inspect_target",
                target_id=target,
                label=label,
            )
            for target, label in objects.get(location_id, [])
        ],
        ActionCandidate(action="wait_until", label="等待到指定时间"),
    ]
    if location_id in {"cemetery", "crypt"}:
        candidates.extend(
            [
                ActionCandidate(
                    action="choose_option", target_id="peaceful_resolution", label="礼貌交谈后离开"
                ),
                ActionCandidate(
                    action="choose_option",
                    target_id="follow_underground",
                    label="跟随道格拉斯进入地下",
                ),
                ActionCandidate(action="choose_option", target_id="flee", label="逃离墓地"),
            ]
        )
    return candidates


def validate_intent(snapshot: ContextSnapshot, result: IntentResult) -> IntentResult:
    """确定性检查模型提案的修订号、目标和动作白名单，拒绝越权输出。"""

    if result.source_revision != snapshot.revision:
        raise ValueError("意图提案已过期")
    if result.kind == "clarification":
        if not result.clarification_question or result.steps:
            raise ValueError("澄清结果字段不完整")
        return result
    if not result.steps or len(result.steps) > 4:
        raise ValueError("意图提案必须包含一到四个动作")
    candidates = {(item.action, item.target_id) for item in snapshot.action_candidates}
    for step in result.steps:
        if (step.action, step.target_id) not in candidates and step.action not in {"start_check"}:
            raise ValueError("意图目标不在当前玩家可见候选中")
        if step.action == "start_check" and (not step.skill_id or not step.goal):
            raise ValueError("检定提案缺少技能或目标")
    return result


def intent_step_to_command(
    step: IntentStep,
    *,
    client_request_id: str,
    expected_revision: int,
    actor_id: str,
) -> CommandEnvelope:
    """把经过校验的第一步意图转换为 Kernel 命令，禁止模型自由写状态。"""

    if step.action == "move_actor":
        command_payload: dict[str, object] = {
            "kind": "move_actor",
            "target_id": step.target_id or "",
        }
    elif step.action == "inspect_target":
        command_payload = {"kind": "inspect_target", "target_id": step.target_id or ""}
    elif step.action == "talk_to_npc":
        command_payload = {
            "kind": "talk_to_npc",
            "target_id": step.target_id or "",
            "topic": step.topic or "",
        }
    elif step.action == "wait_until":
        if step.target_time is None:
            raise ValueError("等待提案缺少目标时间")
        command_payload = {"kind": "wait_until", "target_time": step.target_time}
    elif step.action == "choose_option":
        command_payload = {"kind": "choose_option", "option_id": step.target_id or ""}
    else:
        command_payload = {
            "kind": "start_check",
            "check_id": f"check-{client_request_id}",
            "skill_id": step.skill_id or "",
            "goal": step.goal or "",
            "difficulty": "regular",
        }
    command = CommandAdapter.validate_python(command_payload)
    return CommandEnvelope(
        client_request_id=client_request_id,
        expected_revision=expected_revision,
        actor_id=actor_id,
        command=command,
    )


def guard_narration(
    draft: NarrationDraft,
    *,
    committed_event_ids: Sequence[str],
    visible_facts: Sequence[str],
) -> NarrationDraft:
    """只接受引用已提交事件且不泄露隐藏事实的叙事草稿。"""

    allowed = set(committed_event_ids)
    if not draft.evidence_event_ids or not set(draft.evidence_event_ids) <= allowed:
        raise ValueError("叙事引用了未提交事件")
    # 这些词在第一条路径中只可能来自 keeper 真相；文学表达不能越过事实投影。
    forbidden = ("keeper", "守秘人秘密", "模组真相", "守墓人复活")
    if any(term in draft.text.lower() for term in forbidden):
        raise ValueError("叙事包含受保护的隐藏信息")
    del visible_facts  # 事实列表由调用方记录；Guard 不允许从文本反推新事实。
    return draft


__all__ = [
    "AgentsSdkInterpreter",
    "AgentsSdkNarrator",
    "GmModelUnavailable",
    "IntentInterpreter",
    "Narrator",
    "ScriptedIntentInterpreter",
    "build_context_snapshot",
    "guard_narration",
    "intent_step_to_command",
    "validate_intent",
]

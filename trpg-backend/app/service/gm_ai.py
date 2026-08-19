"""Phase 1B 的上下文、意图解释和叙事边界服务。

本文件只负责把安全快照交给模型并校验模型输出；它不直接修改游戏状态。
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, Protocol

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
                "复制快照 revision。遇到 action=start_check 的候选时，只复制该候选的 action 和"
                "target_id；技能、难度、骰点和成功与否均由服务端决定，不得改成 inspect_target "
                "绕过检定。候选的 label 和 aliases 都可用于匹配玩家原话。"
                "若玩家要求按顺序完成多个动作、中间休息或等到某时再去别处，不得"
                "只挑一步、重排步骤或假装整体完成；必须返回 clarification，请玩家确认"
                "当前先执行的一个有意义边界。"
                "例如‘我先去图书馆，查找关于失踪者的旧资料’包含移动和调查，不能只返回"
                "move_actor；应返回 clarification，说明先移动、到达后再处理调查。"
                "若玩家明确说‘等到/等待到某个具体时间’，必须直接选择 wait_until，"
                "默认玩家留在 snapshot.location_id，不得追问是否移动到其他地点。"
                "若当前只有一个可匹配的 talk_to_npc 候选，玩家已经明确在向该人物说话、"
                "提问或交谈，则必须直接返回 talk_to_npc proposal；语气和具体问题原样概括到"
                "topic，不得因为问题内容不是独立 action 而要求玩家再次确认是否交谈。"
                "不要创建名为 proposal 的嵌套对象。唯一允许的顶层字段是 "
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

        last_error: Exception | None = None
        # 意图生成无副作用；结构化输出偶发不合法时最多重试一次。
        for _attempt in range(2):
            try:
                result = await Runner.run(
                    self._agent,
                    json.dumps(
                        {
                            # 事件 ID 对语义匹配没有作用，不交给模型可减少内部标识泄露面。
                            "snapshot": snapshot.model_copy(
                                update={"recent_event_ids": []}
                            ).model_dump(mode="json"),
                            "player_input": player_input,
                        },
                        ensure_ascii=False,
                    ),
                )
                if not isinstance(result.final_output, IntentResult):
                    raise GmModelUnavailable("意图模型输出类型不正确")
                return result.final_output
            except Exception as exc:  # noqa: BLE001 - 不把供应商细节泄露给玩家。
                last_error = exc
        raise GmModelUnavailable("意图模型调用失败") from last_error


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
                "你是中文克苏鲁跑团叙事器。visible_facts 是本回合唯一且穷尽的"
                "事实来源，必须表达其中每个结果。可以补充不改变事实的感官氛围，但不得"
                "自行断言人物、生物、物品、足迹、入口或线索的存在或不存在，不得补写"
                "NPC 行动、对话、秘密、数值变化或复活。若事实是等待投骰，只承接玩家的"
                "尝试，不得提前宣布成败。使用一至三个简洁完整句，只输出 NarrationDraft。"
                "技能名称只是规则标签，必须按括号中的 purpose 解释；除非 visible_facts "
                "明确声明超自然效果，不得把检定写成法术、灵性力量或身体异常。"
                "snapshot.world_time 是权威当地时间；06:00-11:59 是上午，"
                "12:00-16:59 是午后，17:00-19:59 是傍晚，20:00-05:59 是夜间，"
                "不得使用与该时段矛盾的天色描述。"
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
                        # Narrator 只需要当前公开状态和本回合证据，不需要历史内部事件 ID。
                        "snapshot": snapshot.model_copy(update={"recent_event_ids": []}).model_dump(
                            mode="json"
                        ),
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
        visible_facts=_public_facts_for(session.state_json),
        action_candidates=_candidates_for(session.state_json, actor.location_id),
        recent_event_ids=list(reversed(event_ids)),
    )


def _candidates_for(state: dict[str, Any], location_id: str) -> list[ActionCandidate]:
    """按当前场景生成玩家安全候选，并把模组检定绑定为服务端动作。"""

    runtime = state.get("_runtime", {})
    scene_id = state.get("scene_id")
    checkpoints = (
        [
            item
            for item in runtime.get("checkpoints", [])
            if isinstance(item, dict)
            and item.get("scene_id") == scene_id
            and set(item.get("requires_clues", [])) <= set(state.get("clues", []))
            and _checkpoint_time_available(item, state)
        ]
        if isinstance(runtime, dict)
        else []
    )
    # 同一个模组目标一旦绑定 checkpoint，就不能再以普通检查绕过投骰。
    checked_targets = {
        str(target) for checkpoint in checkpoints for target in checkpoint.get("targets", [])
    }

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
        "kimball_house": [
            ("desk", "检查书桌"),
            ("empty_shelf", "检查空书架"),
            ("surveillance", "监视金博尔宅"),
            ("lock_window", "锁上窗户"),
            ("chase_thief", "追踪破窗后的身影"),
        ],
        "cemetery": [],
        "chase": [("chase_thief", "追踪破窗后的身影")],
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
            if target not in checked_targets
        ],
        ActionCandidate(action="wait_until", label="等待到指定时间"),
    ]
    if isinstance(runtime, dict):
        owned_clues = set(state.get("clues", []))
        # 场景动作只公开标签和稳定目标；执行结果仍由 Kernel 决定。
        for action in runtime.get("actions", []):
            if (
                not isinstance(action, dict)
                or action.get("scene_id") != scene_id
                or not set(action.get("requires_clues", [])) <= owned_clues
            ):
                continue
            action_kind = action.get("action")
            target_id = action.get("target_id")
            label = action.get("label")
            if (
                action_kind not in {"inspect_target", "talk_to_npc", "choose_option"}
                or not isinstance(target_id, str)
                or not isinstance(label, str)
            ):
                continue
            candidates.append(
                ActionCandidate(
                    action=action_kind,
                    target_id=target_id,
                    label=label,
                    aliases=[str(alias) for alias in action.get("aliases", [])],
                )
            )
        for checkpoint in checkpoints:
            checkpoint_id = checkpoint.get("id")
            skill_id = checkpoint.get("skill")
            if not isinstance(checkpoint_id, str) or not isinstance(skill_id, str):
                continue
            # checkpoint 是模组声明的权威检定边界；候选只公开玩家可见语义，
            # 成败线索仍留在服务端冻结运行包中。
            candidates.append(
                ActionCandidate(
                    action="start_check",
                    target_id=checkpoint_id,
                    label=str(checkpoint.get("label") or checkpoint_id),
                    aliases=[str(alias) for alias in checkpoint.get("aliases", [])],
                    skill_id=skill_id,
                )
            )
    return candidates


def _checkpoint_time_available(checkpoint: dict[str, Any], state: dict[str, Any]) -> bool:
    """按会话权威时间判断检定是否可用，支持跨夜时段。"""

    window = checkpoint.get("available_hours")
    if not isinstance(window, dict):
        return True
    start = window.get("start")
    end = window.get("end")
    world_time = state.get("world_time")
    if not isinstance(start, int) or not isinstance(end, int) or not isinstance(world_time, str):
        return False
    hour = datetime.fromisoformat(world_time).hour
    return start <= hour < end if start < end else hour >= start or hour < end


def _public_facts_for(state: dict[str, Any]) -> list[str]:
    """汇总玩家已经获得的公开事实，不把未发现线索或 keeper 数据交给模型。"""

    facts = [str(fact) for fact in state.get("visible_facts", []) if isinstance(fact, str)]
    runtime = state.get("_runtime", {})
    if not isinstance(runtime, dict):
        return facts
    owned_clues = set(state.get("clues", []))
    facts.extend(
        str(clue["text"])
        for clue in runtime.get("clues", [])
        if isinstance(clue, dict)
        and clue.get("visibility") == "public"
        and clue.get("id") in owned_clues
        and isinstance(clue.get("text"), str)
    )
    return list(dict.fromkeys(facts))


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
    candidates = {(item.action, item.target_id): item for item in snapshot.action_candidates}
    bound_steps: list[IntentStep] = []
    for step in result.steps:
        candidate = candidates.get((step.action, step.target_id))
        if candidate is None:
            raise ValueError("意图目标不在当前玩家可见候选中")
        if step.action == "start_check":
            if not candidate.skill_id or not candidate.target_id:
                raise ValueError("模组检定候选缺少服务端绑定")
            # 模型只负责选择 checkpoint；技能和结算目标必须由服务端候选覆盖。
            step = step.model_copy(
                update={"skill_id": candidate.skill_id, "goal": candidate.target_id}
            )
        bound_steps.append(step)
    return result.model_copy(update={"steps": bound_steps})


def guard_intent_coverage(
    snapshot: ContextSnapshot,
    result: IntentResult,
    player_input: str,
) -> IntentResult:
    """单步移动不能静默吞掉玩家明确写出的后续行动。"""

    if result.kind != "proposal" or len(result.steps) != 1:
        return result
    step = result.steps[0]
    if step.action != "move_actor":
        return result
    # 中文复合行动通常以停顿或顺序词连接；后段出现行动动词时必须先向玩家确认边界。
    clauses = [part.strip() for part in re.split(r"[，,；;]|然后|随后|之后|再", player_input)]
    followup_verbs = (
        "查",
        "调查",
        "寻找",
        "询问",
        "观察",
        "检查",
        "交谈",
        "休息",
        "等待",
        "攻击",
        "使用",
    )
    if len(clauses) < 2 or not any(
        any(verb in clause for verb in followup_verbs) for clause in clauses[1:]
    ):
        return result
    candidate = next(
        (
            item
            for item in snapshot.action_candidates
            if item.action == "move_actor" and item.target_id == step.target_id
        ),
        None,
    )
    destination = candidate.label if candidate is not None else "目标地点"
    return IntentResult(
        kind="clarification",
        summary="复合行动需要分步处理",
        clarification_question=(
            f"这包含移动和到达后的行动。现在先执行“{destination}”，到达后再继续吗？"
        ),
        clarification_options=[f"先执行“{destination}”", "重新描述当前行动"],
        source_revision=result.source_revision,
    )


def guard_move_target(
    snapshot: ContextSnapshot,
    result: IntentResult,
    player_input: str,
) -> IntentResult:
    """防止模型把玩家明确要去的地点替换成另一处可达地点。"""

    if result.kind != "proposal" or len(result.steps) != 1:
        return result
    step = result.steps[0]
    if step.action != "move_actor":
        return result
    movement = [
        candidate for candidate in snapshot.action_candidates if candidate.action == "move_actor"
    ]
    selected = next(
        (candidate for candidate in movement if candidate.target_id == step.target_id),
        None,
    )
    if selected is None:
        return result
    text = player_input.strip()
    # “回去/回镇”没有具体地点时，可以使用当前唯一出口；具体地点必须逐字命中
    # 当前候选的玩家可读标签或别名，不能由模型自行把目标改成中间节点。
    return_cues = ("回去", "返回", "回镇", "回到镇上")
    selected_phrases = [selected.label, *selected.aliases]
    selected_matches = any(phrase and phrase in text for phrase in selected_phrases)
    mentioned_other = any(
        any(phrase and phrase in text for phrase in (candidate.label, *candidate.aliases))
        and candidate.target_id != selected.target_id
        for candidate in movement
    )
    if selected_matches or (not mentioned_other and any(cue in text for cue in return_cues)):
        return result
    if mentioned_other or any(verb in text for verb in ("前往", "去", "到", "进入")):
        available = "、".join(candidate.label for candidate in movement)
        return IntentResult(
            kind="clarification",
            summary="地点目标需要确认",
            clarification_question=(
                f"我还不能确认你要前往哪个当前可达地点。请明确描述；目前可前往：{available}。"
            ),
            clarification_options=[candidate.label for candidate in movement[:4]],
            source_revision=result.source_revision,
        )
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
    forbidden_terms: Sequence[str] = (),
) -> NarrationDraft:
    """只接受引用已提交事件且不泄露隐藏事实的叙事草稿。"""

    allowed = set(committed_event_ids)
    if not draft.evidence_event_ids or not set(draft.evidence_event_ids) <= allowed:
        raise ValueError("叙事引用了未提交事件")
    # 这些词在第一条路径中只可能来自 keeper 真相；文学表达不能越过事实投影。
    # Narrator 面向玩家，不得复述解释器候选、内部字段或系统约束。
    forbidden = (
        "keeper",
        "action_candidates",
        "行动列表",
        "可选行动",
        "下一步行动",
        "请从选项中选择",
        "请根据你的角色和处境",
        "只能选择列表",
        "不能自行创建新行动",
        "守秘人秘密",
        "模组真相",
        "守墓人复活",
    )
    if any(term in draft.text.lower() for term in forbidden):
        raise ValueError("叙事包含受保护的隐藏信息")
    # 下划线标识只属于运行包和内部动作协议；中文主持叙事不应把它们回显给玩家。
    if re.search(r"(?<![A-Za-z])[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+(?![A-Za-z])", draft.text):
        raise ValueError("叙事包含内部标识")
    # 正常回合被约束为一至三个短句；异常长文通常意味着模型开始扩写场景或复述候选。
    if len(draft.text) > 360:
        raise ValueError("叙事超出玩家安全长度")
    if draft.text.rstrip().endswith(("：", ":")):
        raise ValueError("叙事是未完成句段")
    # 具体禁词由冻结模组按线索解锁关系提供，主持代码不认识任何剧情专名。
    if any(term and term in draft.text for term in forbidden_terms):
        raise ValueError("叙事包含尚未公开的模组信息")
    # 这些是新增可观察事实的常见句式。只有当本回合证据明确包含该句式时，
    # Narrator 才能使用它；否则模型不能借“文学描写”凭空增加物品、动作或秘密。
    grounded_text = " ".join(str(fact) for fact in visible_facts)
    ungrounded_observation_markers = (
        "手中的",
        "手里拿着",
        "手上的",
        "手里的",
        "摆弄",
        "拍了拍",
        "扫向",
        "目光",
        "藏着",
        "藏有",
        "秘密",
        "传来",
        "延伸向",
        "蹲在",
        "站着",
        "站在",
        "躲在",
        "通向",
        "半掩着",
    )
    if any(
        marker in draft.text and marker not in grounded_text
        for marker in ungrounded_observation_markers
    ):
        raise ValueError("叙事增加了未被证据支持的观察或动作")
    # 时长会改变玩家对事件顺序的理解；没有出现在公开事实中的时长不得由模型自行补写。
    duration_pattern = re.compile(
        r"(?:\d+|[一二三四五六七八九十百半几数]+)(?:分钟|小时|天|日|周|个月|月|年)"
    )
    grounded_durations = {
        match.group(0) for fact in visible_facts for match in duration_pattern.finditer(fact)
    }
    claimed_durations = {match.group(0) for match in duration_pattern.finditer(draft.text)}
    if not claimed_durations <= grounded_durations:
        raise ValueError("叙事包含无证据的时间长度")
    # 检定尚未投骰时，Narrator 只能承接动作，不能抢先宣布权威结果。
    awaiting_roll = any("准备" in fact and "检定" in fact for fact in visible_facts)
    if awaiting_roll and any(term in draft.text for term in ("成功", "失败", "骰点")):
        raise ValueError("叙事在投骰前声明了检定结果")
    del visible_facts  # 事实列表由调用方记录；Guard 不允许从文本反推新事实。
    return draft


def guard_clarification(
    result: IntentResult,
    *,
    forbidden_terms: Sequence[str] = (),
) -> IntentResult:
    """阻止意图模型借澄清问题向玩家泄露内部标识或未公开剧情。"""

    if result.kind != "clarification" or not result.clarification_question:
        return result
    text = " ".join([result.clarification_question, *result.clarification_options])
    protected = (
        "keeper",
        "action_candidates",
        "target_id",
        "scene_id",
        "checkpoint",
        "行动列表",
        "只能选择列表",
        "不能自行创建新行动",
    )
    unsafe = (
        len(result.clarification_question) > 240
        or len(result.clarification_options) > 4
        or any(len(option) > 80 for option in result.clarification_options)
        or any(term in text.lower() for term in protected)
        or any(term and term in text for term in forbidden_terms)
    )
    if not unsafe:
        return result
    # 模型澄清越界时不回显任何候选或模组内容，只保留一个可继续游玩的安全问题。
    return result.model_copy(
        update={
            "clarification_question": "我还不能确定你现在想做什么，请换一种方式描述当前行动。",
            "clarification_options": [],
        }
    )


__all__ = [
    "AgentsSdkInterpreter",
    "AgentsSdkNarrator",
    "GmModelUnavailable",
    "IntentInterpreter",
    "Narrator",
    "ScriptedIntentInterpreter",
    "build_context_snapshot",
    "guard_clarification",
    "guard_intent_coverage",
    "guard_move_target",
    "guard_narration",
    "intent_step_to_command",
    "validate_intent",
]

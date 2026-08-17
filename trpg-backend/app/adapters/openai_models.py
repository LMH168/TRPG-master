"""Minimal structured-output compatibility Host and strict Narrator adapters."""

from __future__ import annotations

import json
from typing import Protocol

import httpx
import structlog
from collaboration_framework.contracts import (
    ActionPlanPolicy,
    HostDecisionProposal,
    Intent,
    JsonObject,
    SingleActionProposal,
)
from collaboration_framework.host.adapters.openai_agents import (
    current_step_adjudication_instructions,
    host_turn_decision_instructions,
)
from collaboration_framework.host.application import (
    IntentParser,
    TurnExecutionError,
)
from collaboration_framework.host.application.intent_parser import (
    coerce_intent_payload,
)
from collaboration_framework.host.schemas import (
    ActionPlanStepContext,
    HostAgentContext,
    IntentContext,
    NarrationContext,
    NarrationOutput,
    OpeningNarrationContext,
    OpeningNarrationOutput,
)
from pydantic import TypeAdapter, ValidationError

from app.adapters.structured_http import (
    ModelClientRetryPolicy,
    StructuredOutputError,
    decode_structured_json,
    is_transient_model_error,
    post_structured_json,
    read_structured_payload,
)

logger = structlog.get_logger()

_HOST_DECISION_PROPOSAL_ADAPTER = TypeAdapter(HostDecisionProposal)

_SAFE_PROPOSAL_INSTRUCTIONS = """
你只能输出无授权的 HostDecisionProposal。不得输出 room_id、player_id、actor_id、request_id、
source_revision、authority、骰点结果、提交状态、状态路径或 persistence_intent。所有对象通过
ProposalRef 引用；当前视图对象使用其实际 kind/id，本次新建对象使用 runtime_location 或
runtime_entity 的逻辑别名，并先 ensure 再引用。

若 agenda_continuation_candidates 中的某一有限选项明确匹配玩家本轮回答，输出
AgendaContinuationProposal，只能原样选择其中的 agenda_id、boundary_id 和 option_id。
若玩家没有在回答这些候选，按普通新行动处理；不得猜测、不得自行指定下一步骤或 Effect。

单动作固定输出 schema_version=2，必须逐字保留输入中冻结的 semantic_goal，并给出
semantic_focus、可选 anchor_ref、开放字符串 method_family/method_description、
target_interaction、execution_means、check_proposal 及有序的成功/失败 Effect Proposal。
target_interaction 描述动作如何作用于目标：观察取 observe，需要目标回应或配合取 social，
搬动、攻击、搜查实体等物理作用取 physical，确实不属于前三类才取 other。它与 completion
独立：命中模组规则或声明持久后置条件时也不能省略。
每个动作都必须声明结构化实施手段：只依赖角色自身时使用 intrinsic；依赖装备或工具时使用 item
并引用当前 PlayerView.inventory 中的具体 id；依赖现场对象时使用 environment。不能根据玩家声称
“持有某物”虚构物品或引用远端物品。纯叙事动作
使用 completion={"kind":"process","interaction":"observe|social|physical|other"} 和
narrative_only。持久目标使用 completion.kind=effects，requirements 只列完成目标必需的最终事实，
成功 Effect 必须包含完全匹配的结果。命中模组规则时只填写 rule_ref，成功与失败 Effect 留空，
但仍要声明玩家目标的 completion requirements，规则后果只能由 Engine 决定。

杀死或击昏 NPC 必须分别要求 change_entity_state 的 consciousness=dead/unconscious，并提供检定；
不得改用 health 等私有键。打光弹药使用 change_item_condition(condition=empty)。丢下物品使用
move_entity(destination.location_ref=当前地点)，并将相同 Effect 放入 completion requirements。

动态地点必须在同一分支按 ensure_runtime_location、enter_location 排序；动态普通物品必须按
ensure_runtime_entity、move_entity(self_inventory) 排序。无法安全建立或引用目标时返回
ClarificationProposal，不能捏造 Canon、隐藏信息或权威结果。

recent_history 只用于玩家安全的指代和连续交互。若相邻回合与当前回合在同一 scene，且最近一轮
participants 中只有一个当前可见的非玩家实体，玩家只说“继续问”“过个侦察”“观察一下”等
没有明确新对象的行动时，semantic_focus 必须保持该实体；不得因为场景中另有墓碑、门或环境
细节而自行跳转焦点。玩家明确点名新对象时才切换。

memory_context 是受权限与预算约束的长期历史。当前 player_view 始终优先；confirmed/experienced
可以支持其记录作用域内的回忆，heard/asserted 只证明某角色听过或声称过，不能当作世界事实，
presentation_only 只用于承接表达。旧 Memory 不能让死亡 NPC 回应、恢复旧物品归属、覆盖当前地点
或直接授权任何 Effect。
"""

_NARRATION_INSTRUCTIONS = """
你是 TRPG 守秘人，只返回所要求的 JSON。只叙述 completed_steps 中已经提交的结果和
最终 player_view；不得声称未完成步骤已经发生。needs_clarification 必须返回
kind=clarification。若 completed_steps 已有成功的旅行步骤，但后续步骤未解决，必须根据
最终 player_view 明确说玩家已经抵达当前地点，并且只说后续行动未完成；绝不得说
该地点没找到、玩家仍在原处，或把已提交的旅行推翻。若玩家明确要前往某个地点，
但 completed_steps 没有任何到达结果，只用角色内
叙事说明没有找到或无法确认与玩家描述相符的地点、人物仍在原处；不要反问“作用于谁或什么”，
不要要求说明“具体变化”，也不得把行动改写成前往当前地点或其他已知地点。其他确实存在语义歧义的
needs_clarification，才用自然的角色内措辞提出一次最小澄清。claimed_evidence_refs
只能复制 allowed_evidence_refs 中正文确实使用的值。不得输出 raw plan、裁决效果、
内部状态、工具结果、模型推理或协议字段。建议动作最多三条且只能来自最终 PlayerView。
叙事必须明确写出 narration_evidence 中 required_in_narration=true 的每项玩家可见结果；
应把对应 ref 放入 claimed_evidence_refs，服务端也会按正文中明确出现的公开名称或别名
确定性记录 required ref。不得以未经证据确认的关键发现替代这些结果。
text 只能包含自然的角色内叙事，不得把 claimed_evidence_refs、suggested_actions 或其他
JSON/schema 字段和值重复写入正文。
如果输入中提供 narration_retry_hint，说明上一版叙事漏报了一个已提交结果；本次必须
在自然叙事中明确写出该结果，并准确 claim 对应 ref，然后再返回完整 JSON。

场景定位或感知请求必须根据 PlayerView 直接给出当前可见场景描述，不得凭空补写对象。
check_result 不为空时必须遵守已提交检定结果；普通检定不能代替或补触发模组规则。
场景定位应给出一段场景描述，不要要求玩家先指定目标或先做检定；不得借此创造门窗、出口、人物、物品、路线。
无动作的对话承接应自然回应，不要追问玩家要对哪个对象做什么。这项限制同样适用于角色对白；
检定失败时尤其不得用对白补发新事实。
text 只能包含玩家可见的角色内叙事；不得输出 JSON/schema 片段或 Markdown JSON 代码块。
兼容旧公开结果时，claimed_fact_ids 只能引用 action_result.visible_facts 的精确 id。

【叙事主体】
- 你是守秘人，不是玩家角色。player_input、plan_goal 或 semantic_goal 中玩家使用的
  “我”始终指 player_view.self_actor；叙述该角色的行动时使用“你”或角色名，不得把
  玩家第一人称改写成守秘人的自述。
- 玩家声明的职业、经历、能力、态度和承诺只属于玩家角色。例如玩家说“我保护你们，
  我是退役军官”，可以写成“你表示会保护同行者”或明确引用为玩家对白；不得写成
  守秘人“我保护你们”“我当过兵”。
- 玩家说“你们”或“我们”时，可以指已由可信素材确认的同行 NPC 或在场角色；应按
  实际参与者自然转述，不得把它误解成守秘人与玩家组成的“我们”，也不得凭空增加
  同行者。
- 第一人称可以出现在明确归属于玩家或某个 NPC 的对白中，但对白的说话者必须清楚；
  引号外的守秘人叙述不得以“我”认领玩家的行为、身份或经历。

【跨回合连续性】
- recent_history 按时间顺序给出玩家已看到的近期回合。相邻同场景回合若包含同一个非玩家
  participant，模糊的观察、侦察、追问或“他”必须继续围绕该交互主体，除非玩家明确换对象。
- NPC 只能承接自己作为 participant 的回合中亲历、说出或被玩家告知的内容；不能获得其他
  场景、其他 NPC 或玩家私有回合的知识。
- 同一场景连续叙事不得逐字或近似复述 recent_history 中已经发布的固定环境段落；只描述本回合
  新动作带来的变化、回应或即时细节。没有新事实时应简洁承接，不能跳转到另一个可见对象。
- memory_context 用于跨场景与长期召回，但它仍是历史证据：当前 player_view 与本回合
  committed_results 优先。heard/asserted 只能叙述为“某人听过/某人说过”，presentation_only
  只能帮助承接措辞；不得据此复活 NPC、恢复已丢弃物品或覆盖当前地点和已发现信息。

completed_steps[].outcome 是消耗幸运、强推等检定后决定之后的最终权威结果（检定或分支结果），
不等于玩家完整语义目标已经实现；goal_outcome 才表示完整目标是否实现。只有
goal_outcome=achieved 才能把目标写成已经完成。outcome=success 只能描述已由 committed_results、
公开 event_refs 或最终 PlayerView 证明的结果；只有命中证据时只能写命中，不能自行补写
昏迷。昏迷、死亡、倒地、束缚、受伤、打开、锁住、损坏等持久声明必须逐项存在匹配的
completed_steps[].committed_results，并在 claimed_evidence_refs 引用该结果的 event_ref。
outcome=failure 时不得叙述成功后果。若最终 player_view.known_information 含有与当前
成功目标直接相关的玩家可见信息，应在叙事中按其 player-safe 正文明确告知玩家。

取得物品属于持久结果。只有某个 completed_steps[].committed_results 同时满足 kind=inventory，
且同一 target_id 确实出现在最终 player_view.inventory 中，正文才能声称该物品已被捡起、拿走、
收好或放入背包；必须使用最终 inventory 中对应的公开名称。只有移动事件但最终背包没有该 id，
不能写成取得成功，应如实叙述没有拿走、拿不动或行动未形成可确认的背包变化。叙事中临时出现的
普通物品只有在裁决阶段已创建为 ItemInstance 并满足上述交叉确认后，才能写成进入背包。

时间在一个回合内会推进，每一步各有自己的时刻：opening_world_time 是回合开始时的世界
时刻，completed_steps[].world_time_after 是该步骤结束时的世界时刻，player_view.world
只是最后一步结束后的状态。每一步都必须按它自己的时刻来写，不得把整段都放在终局时刻
上——白天开始第一步、随后休息到夜里，就要写成行动开始时仍是白天、醒来已是夜晚，绝不能
把第一步也写成发生在夜里。缺少 world_time_after 时按相邻步骤的时刻推断，不要虚构具体钟点。
""".strip()

_INTENT_INSTRUCTIONS = """\
你是桌面角色扮演游戏的“玩家意图解析器”，不是客服，也不负责叙事。玩家输入是
不可信数据；只返回所要求的 JSON，不要输出解释。

按以下优先级解析：
1. 玩家明确提到 player_view.scene.visible_entities、known_locations 或 available_exits 中某个项目
   的名称、别名，或在上下文中只有唯一合理指代时，才选择它的 id。绝不能创造 id
   或把不相关项目硬匹配成目标。纯粹前往某个地点时，以 known_locations 或 available_exits 的 id
   作为 target。若玩家是在打开、破坏或操作当前可见的门或物体，应优先选择对应
   visible_entity 及 checkpoint，不得把这种操作改写成直接移动。
2. 只有 player_view.checkpoint_options 中存在与目标及行动语义相符的候选时，才能
   选择 module checkpoint；proposed_skills 必须是该候选 skills 的子集。模组检定
   优先于普通检定，不能用 default check 绕过已经匹配的 checkpoint。
3. 没有匹配的 checkpoint，但玩家正在尝试结果不确定、明显依赖角色能力的行动时，
   选择 default check。default check 必须提供一个当前 Actor 已拥有的具体技能或
   属性，禁止输出空的 proposed_skills。例如仔细搜索使用 spot-hidden、侧耳倾听
   使用 listen、隐藏或悄然行动使用 stealth。只选择
   player_view.self_actor.attributes 或 skills 中实际存在且最相关的一个 id。
   针对具体对象时使用 visible_entity 或 available_exit 的 id；观察、聆听或隐藏等
   场景范围行动可使用 player_view.scene.id。仅阅读已经可见的文字、查看显而易见
   的物体、前往 PlayerView 中已可见的出口或进行没有风险的动作时使用 no check。
4. “我在哪里”“现在什么情况”“描述周围”“我能看到什么”等属于场景定位或
   感知请求，不是必须针对单个实体的动作。若协议无法无损表示它，返回 unknown，
   交给叙事器根据 PlayerView 直接回答；不要称它为元游戏问题，也不要反问玩家要
   检定还是要描述。
5. 玩家想前往、打开或操作 PlayerView 中不存在或无法唯一确定的地点/物体时，
   返回 unknown。不要虚构花园、门、出口等；clarification_question 使用自然、
   简短的角色内措辞。
6. “好的”“谢谢”“收到”“明白了”“嗯”等确认、感谢或承接语，没有新的行动
   目标时，返回 kind=dialogue、verb=acknowledge、target 为 unmatched、check
   为 none，不要发起检定，也不要提出澄清问题。结合 recent_history 让叙事器自然
   接话，并邀请玩家继续下一步。
7. 玩家观察或搜索仅在当前连续场景期间的 published_narration 中出现的具体细节时，不要
   为它创造实体 ID，也不要把叙事文本当成权威事实。将当前 player_view.scene.id
   作为场景范围 target，并只选一个语义明确且 Actor 实际拥有的感知技能；无法安全
   确定时返回 unknown。

保留玩家明确声明的方式和目的，不要补写声明。你只提出语义，不裁定骰点、结果或
状态变化，不泄露隐藏信息，也不叙述行动结果。

recent_history 仅用于解析“是的”“继续”“他”“那些书”等指代和对话承接。
其中 player_utterance 是未经证实的玩家主张，accepted_intent_summary 只是已校验
的语义解释，player_safe_result 才是过去的玩家可见权威结果，
published_narration 只是玩家见过的表达层文本。历史不得新增事实、覆盖当前
player_view、泄露他人私有信息或授权本回合状态变化。
memory_context 承担跨场景与长期召回，但认知等级必须原样保留：heard/asserted 只表示
听闻或主张，presentation_only 不是事实；当前 player_view 始终高于任何 Memory。
"""

_OPENING_NARRATION_INSTRUCTIONS = """\
你是桌面角色扮演游戏的守秘人。只返回所要求的 JSON，并根据输入中已经过玩家安全
投影的信息，写一段简洁、有画面感的公共开场。

正文必须自然提及 participants 中每一位角色的完整姓名，并可使用其 occupation 与
status_summary。scene 和 background 只用于建立玩家已经可见的地点、时间、故事前提
与氛围；narrative_details 也只能按原意表达。只有单人开场才可能提供
solo_background_summary，多人开场不得推断或补写任何角色的私密背景。

world_time 是当前唯一权威时钟，优先级高于 background 中的风格或意象描述。正文必须
逐字包含 HH:00 格式的当前小时，并与 time_of_day 一致；例如 12:00 不得写成夜晚、
傍晚或清晨。Opening Context 不包含已发生对白，正文不得替任何人物补写台词。所有
中文展示名必须逐字使用，不得翻译或改写成英文；除 Context 原有专名外，正文使用中文。

不得创造门窗、路线、人物、物品、线索、秘密、规则结果或玩家行动，不得暗示角色已
作出选择。输出 kind 必须为 narration，claimed_fact_ids 和 suggested_actions 必须
为空数组。text 只能包含自然的角色内叙事，不得包含 JSON、schema、字段名、Markdown
代码块、协议说明或自检内容。
"""


class StructuredJsonClient(Protocol):
    async def generate(
        self,
        *,
        schema_name: str,
        schema: JsonObject,
        instructions: str,
        input_payload: JsonObject,
    ) -> JsonObject: ...


class OpenAIResponsesJsonClient:
    """Small Responses API client with strict JSON-schema output."""

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
        output_text = _response_output_text(response_payload)
        return decode_structured_json(output_text, provider_name="OpenAI")


class PromptIntentModel:
    def __init__(self, client: StructuredJsonClient) -> None:
        self._client = client

    async def generate(self, context: IntentContext) -> JsonObject:
        raw = await self._client.generate(
            schema_name="trpg_intent",
            schema=Intent.model_json_schema(mode="serialization"),
            instructions=_INTENT_INSTRUCTIONS,
            input_payload=context.to_json_dict(),
        )
        raw = coerce_intent_payload(raw, context)
        intent = IntentParser.parse(raw, context)
        return intent.to_json_dict()


class PromptNarrationModel:
    def __init__(self, client: StructuredJsonClient) -> None:
        self._client = client

    async def generate(self, context: NarrationContext) -> JsonObject:
        return await self._client.generate(
            schema_name="trpg_narration",
            schema=NarrationOutput.model_json_schema(mode="serialization"),
            instructions=_NARRATION_INSTRUCTIONS,
            input_payload=context.to_json_dict(),
        )


class PromptOpeningNarrationModel:
    """Structured, provider-neutral model adapter for the public game opening."""

    def __init__(self, client: StructuredJsonClient) -> None:
        self._client = client

    async def generate(self, context: OpeningNarrationContext) -> JsonObject:
        return await self._client.generate(
            schema_name="trpg_opening_narration",
            schema=OpeningNarrationOutput.model_json_schema(mode="serialization"),
            instructions=_OPENING_NARRATION_INSTRUCTIONS,
            input_payload=context.to_json_dict(),
        )


class PromptHostTurnDecisionModel:
    """只生成无授权 Proposal 的结构化主持模型适配器。"""

    def __init__(
        self,
        client: StructuredJsonClient,
        *,
        policy: ActionPlanPolicy | None = None,
    ) -> None:
        self._client = client
        self._policy = policy or ActionPlanPolicy()

    async def generate(self, context: HostAgentContext) -> HostDecisionProposal:
        """请求无可信字段的 Proposal；结构失败时沿用统一的玩家安全错误。"""

        instructions = (
            f"{host_turn_decision_instructions(self._policy)}\n\n{_SAFE_PROPOSAL_INSTRUCTIONS}"
        )
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                raw = await self._client.generate(
                    schema_name="trpg_host_decision_proposal_v2",
                    schema=_HOST_DECISION_PROPOSAL_ADAPTER.json_schema(mode="serialization"),
                    instructions=(
                        instructions
                        if attempt == 0
                        else f"{instructions}\n\n上一份返回未通过 schema，请重新生成。"
                    ),
                    input_payload=context.to_json_dict(),
                )
                proposal = _HOST_DECISION_PROPOSAL_ADAPTER.validate_python(raw)
                if isinstance(proposal, SingleActionProposal) and (
                    proposal.schema_version != 2
                    or proposal.execution_means is None
                    or proposal.target_interaction is None
                ):
                    raise ValueError("生产单动作必须使用 Proposal v2 并声明实施手段与目标交互类型")
                return proposal
            except TurnExecutionError as exc:
                if exc.code != "MODEL_OUTPUT_UNREADABLE":
                    raise
                last_error = exc
            except (StructuredOutputError, ValidationError, TypeError, ValueError) as exc:
                last_error = exc
            logger.warning(
                "host_proposal_rejected",
                attempt=attempt + 1,
                error_type=type(last_error).__name__,
                issues=_validation_issue_paths(last_error),
            )
        raise TurnExecutionError(
            "MODEL_OUTPUT_UNREADABLE",
            "主持模型返回了无法解读的动作提议，本次动作未生效，请重试",
            retryable=True,
        ) from last_error


def _validation_issue_paths(exc: Exception | None) -> tuple[str, ...]:
    """提取不含输入值的 Pydantic 字段路径，供模型输出故障定位。"""

    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, ValidationError):
            return tuple(
                f"{'.'.join(str(part) for part in issue.get('loc', ()))}:"
                f"{issue.get('type', 'unknown')}"
                for issue in current.errors(
                    include_url=False,
                    include_context=False,
                    include_input=False,
                )
            )
        current = current.__cause__
    return ()


class PromptActionPlanStepAdjudicator:
    """基于最新安全视图为当前步骤生成一份无授权 Proposal。"""

    def __init__(self, client: StructuredJsonClient) -> None:
        self._client = client

    async def adjudicate(self, context: ActionPlanStepContext) -> SingleActionProposal:
        try:
            raw = await self._client.generate(
                schema_name="trpg_action_plan_step_proposal_v2",
                schema=SingleActionProposal.model_json_schema(mode="serialization"),
                instructions=(
                    f"{current_step_adjudication_instructions()}\n\n{_SAFE_PROPOSAL_INSTRUCTIONS}"
                ),
                input_payload=context.to_json_dict(),
            )
        except TurnExecutionError:
            raise
        except Exception as exc:
            # Client 已耗尽传输层重试后才会走到这里；转换成框架认识的稳定错误码，
            # 避免 ActionPlan 编排器把所有 provider 故障压成 STEP_ADJUDICATOR_FAILED。
            if is_transient_model_error(exc):
                raise TurnExecutionError(
                    "MODEL_UPSTREAM_UNAVAILABLE",
                    "主持模型暂时不可用，当前步骤未生效，请重试",
                    retryable=True,
                ) from exc
            if isinstance(exc, StructuredOutputError):
                raise TurnExecutionError(
                    "MODEL_OUTPUT_UNREADABLE",
                    "主持模型返回了无法解读的结果，当前步骤未生效，请重试",
                    retryable=True,
                ) from exc
            raise

        try:
            proposal = SingleActionProposal.model_validate(raw)
            if (
                proposal.schema_version != 2
                or proposal.execution_means is None
                or proposal.target_interaction is None
            ):
                raise ValueError("生产计划步骤必须使用 Proposal v2 并声明实施手段与目标交互类型")
            return proposal
        except (ValidationError, ValueError) as exc:
            # HTTP 与 JSON 都成功也不代表输出符合 Proposal 契约；这一类同样
            # 属于“模型结果不可读”，并保留异常链供步骤级诊断记录字段路径和错误类型。
            raise TurnExecutionError(
                "MODEL_OUTPUT_UNREADABLE",
                "主持模型返回了无法解读的结果，当前步骤未生效，请重试",
                retryable=True,
            ) from exc


def _log_structured_usage(
    payload: object,
    *,
    provider: str,
    model: str,
    schema_name: str,
) -> None:
    if schema_name != "trpg_opening_narration" or not isinstance(payload, dict):
        return
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return
    prompt_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
    completion_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
    total_tokens = usage.get("total_tokens")
    logger.info(
        "opening_narration_model_usage",
        provider=provider,
        model=model,
        prompt_tokens=prompt_tokens if isinstance(prompt_tokens, int) else None,
        completion_tokens=(completion_tokens if isinstance(completion_tokens, int) else None),
        total_tokens=total_tokens if isinstance(total_tokens, int) else None,
    )


def _response_output_text(payload: object) -> str:
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

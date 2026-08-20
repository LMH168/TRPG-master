"""Phase 0 GM Kernel 的会话安装、Wait 命令和幂等回执服务。"""

import copy
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.dto.gm import (
    CheckRead,
    ClarificationRead,
    CommandEnvelope,
    CommandResult,
    DomainEventEnvelope,
    GmTurnRead,
    KnownLocationRead,
    PendingDecision,
    PlayerProjection,
    SessionRead,
    TurnInputBody,
)
from app.models.content import Scenario
from app.models.gm import (
    CheckRun,
    CommandReceipt,
    GameEvent,
    GameSession,
    ModuleVersion,
    OutboxMessage,
    PendingDecisionRecord,
    RuntimeActor,
    TurnRun,
)
from app.models.room import Character
from app.service.gm_ai import (
    AgentsSdkInterpreter,
    AgentsSdkNarrator,
    GmModelUnavailable,
    IntentInterpreter,
    Narrator,
    build_context_snapshot,
    guard_clarification,
    guard_intent_coverage,
    guard_move_target,
    guard_narration,
    intent_step_to_command,
    validate_intent,
)
from app.service.module_runtime import ModulePackError, load_preset


class GmRuntimeError(ValueError):
    """GM 会话或命令无法通过 Kernel 校验。"""


_intent_interpreter: IntentInterpreter | None = None
_narrator: Narrator | None = None


def set_agents_for_testing(
    interpreter: IntentInterpreter | None,
    narrator: Narrator | None = None,
) -> None:
    """仅测试时注入脚本模型，生产路径始终从 Settings 创建真实 provider。"""

    global _intent_interpreter, _narrator
    _intent_interpreter = interpreter
    _narrator = narrator


def _agents() -> tuple[IntentInterpreter, Narrator]:
    """获取真实模型 Agent；没有通过配置时让回合进入可恢复失败。"""

    global _intent_interpreter, _narrator
    if _intent_interpreter is None:
        settings = get_settings()
        _intent_interpreter = AgentsSdkInterpreter(settings)
    if _narrator is None:
        settings = get_settings()
        _narrator = AgentsSdkNarrator(settings)
    return _intent_interpreter, _narrator


# Phase 1A 只冻结《追书人》第一条单人调查需要的对象；后续完整 ModulePack
# 会把同样的结构移入模组运行包，Kernel 仍只消费结构化定义。
_LOCATIONS = {
    "arnoldsburg": {"library", "newspaper", "kimball_house", "cemetery"},
    "library": {"arnoldsburg", "newspaper"},
    "newspaper": {"arnoldsburg", "library"},
    "kimball_house": {"arnoldsburg", "cemetery"},
    "cemetery": {"arnoldsburg", "kimball_house", "crypt"},
    "crypt": {"cemetery"},
}
_TARGETS = {
    "arnoldsburg": {
        "town_sign",
        "neighbors",
        "library",
        "newspaper",
        "kimball_house",
        "cemetery",
    },
    "library": {"old_newspapers", "bookshelf", "librarian"},
    "newspaper": {"newspaper_archive", "hilda"},
    "kimball_house": {
        "desk",
        "empty_shelf",
        "search_study",
        "read_diary",
        "surveillance",
        "lock_window",
        "chase_thief",
    },
    "cemetery": {
        "graveyard_gate",
        "headstone",
        "gravekeeper",
        "track_grave",
        "night_watch",
        "open_crypt",
        "call_douglas",
        "attack_douglas",
        "fight_ghouls",
        "leave",
    },
    "crypt": {"open_crypt", "follow_douglas", "talk_douglas", "leave"},
}
_SKILLS = {
    "spot-hidden": {"base": 25, "purpose": "发现不明显的物体、痕迹或异常"},
    "library-use": {"base": 20, "purpose": "检索和理解图书馆、报纸与档案资料"},
    "listen": {"base": 20, "purpose": "发现听觉上不明显的声音或动静"},
    "persuade": {"base": 10, "purpose": "以合理承诺或论证说服他人"},
    "charm": {"base": 15, "purpose": "以友善态度建立短暂信任"},
    "sanity": {"base": 50, "purpose": "面对超自然现象时保持理智"},
}


async def create_session(
    db: AsyncSession,
    *,
    room_id: str,
    module_id: str,
    actor_id: str,
    display_name: str,
) -> SessionRead:
    """安装固定版本 ModulePack，并创建首个调查员 Actor。"""

    scenario = await db.scalar(select(Scenario).where(Scenario.module_id == module_id))
    if scenario is None:
        raise GmRuntimeError(f"目录中不存在模组：{module_id}")
    try:
        pack = load_preset(module_id)
    except ModulePackError as exc:
        raise GmRuntimeError(str(exc)) from exc
    existing = await db.get(GameSession, room_id)
    if existing is not None:
        actor = await db.get(RuntimeActor, actor_id)
        if actor is None or actor.room_id != room_id:
            raise GmRuntimeError("该房间已经创建了其他 GM 会话")
        return _session_read(existing, actor)

    module_version = await db.get(ModuleVersion, (scenario.id, pack.version))
    if module_version is None:
        candidate = ModuleVersion(
            module_id=scenario.id,
            version=pack.version,
            world_ref="coc7",
            content_schema_version=1,
            content_hash=pack.content_hash,
            # 目录用于房间展示，runtime 是同一版本的结构化规则切片。
            content_json={"catalog": pack.catalog, "runtime": pack.runtime},
        )
        try:
            # React StrictMode 和重试都可能并发进入这里；保存点只回滚这次插入，
            # 不影响外层请求事务，冲突后重新读取赢家写入的同一份版本快照。
            async with db.begin_nested():
                db.add(candidate)
                await db.flush()
            module_version = candidate
        except IntegrityError:
            module_version = await db.get(ModuleVersion, (scenario.id, pack.version))
            if module_version is None:
                raise
    now = datetime.now(UTC)
    runtime = pack.runtime
    initial = dict(runtime.get("initial_state", {}))
    initial_location = str(runtime.get("initial_location_id", "arnoldsburg"))
    initial_world_time = str(runtime.get("initial_world_time", now.isoformat()))
    # GM Actor 从已完成角色卡初始化；旧测试和历史房间继续使用模组默认值。
    character = await db.scalar(
        select(Character).where(
            Character.room_id == room_id,
            Character.player_id == actor_id,
            Character.status == "complete",
        )
    )
    derived = (character.derived_stats or {}) if character is not None else {}
    attributes = (character.attributes or {}) if character is not None else {}
    skills = (character.skills or {}) if character is not None else {}
    equipment = (character.equipment or []) if character is not None else []
    session = GameSession(
        room_id=room_id,
        module_id=scenario.id,
        module_version=pack.version,
        content_hash=pack.content_hash,
        ruleset_version="source-1",
        ruleset_profile="paper_chase_phase1c",
        state_schema_version=1,
        state_json={
            "world_time": initial_world_time,
            "location_id": initial_location,
            "known_locations": [initial_location],
            "visible_facts": [],
            "scene_id": runtime.get("initial_scene_id", "briefing"),
            "clues": list(initial.get("clues", [])),
            "flags": dict(initial.get("flags", {})),
            "ending_id": None,
            # 运行包随会话冻结在数据库中；投影函数只挑选公开字段，不会返回此私有定义。
            "_runtime": runtime,
        },
        state_version=0,
        created_at=now,
        updated_at=now,
    )
    actor = RuntimeActor(
        id=actor_id,
        room_id=room_id,
        display_name=(character.name or display_name) if character is not None else display_name,
        location_id=initial_location,
        state_json={
            "alive": True,
            "hp": int(derived.get("HP", initial.get("hp", 10))),
            "san": int(derived.get("SAN", initial.get("san", 50))),
            "luck": int(attributes.get("LUCK", 50)),
            "skills": dict(skills),
            "items": list(equipment or initial.get("items", [])),
            "character_id": character.id if character is not None else None,
        },
        created_at=now,
    )
    try:
        db.add_all([session, actor])
        await db.flush()
    except IntegrityError:
        # 两个首次请求也可能同时创建 GameSession/Actor；回滚本次候选写入后，
        # 复用已经提交的会话，保证重试只返回同一份权威状态而不产生第二局。
        await db.rollback()
        existing = await db.get(GameSession, room_id)
        existing_actor = await db.get(RuntimeActor, actor_id)
        if existing is None or existing_actor is None or existing_actor.room_id != room_id:
            raise
        return _session_read(existing, existing_actor)
    await db.commit()
    return _session_read(session, actor)


async def submit_command(
    db: AsyncSession,
    *,
    room_id: str,
    envelope: CommandEnvelope,
    narrate: bool = False,
) -> CommandResult:
    """在一个数据库事务中校验 revision、应用 Wait 并写入事件/回执/Outbox。"""

    session = await db.scalar(
        select(GameSession).where(GameSession.room_id == room_id).with_for_update()
    )
    actor = await db.get(RuntimeActor, envelope.actor_id)
    if session is None or actor is None or actor.room_id != room_id:
        raise GmRuntimeError("GM 会话或调查员不存在")
    receipt = await db.get(CommandReceipt, (room_id, envelope.client_request_id))
    if receipt is not None:
        return CommandResult.model_validate(receipt.result_json)
    if envelope.expected_revision != session.state_version:
        raise GmRuntimeError("revision 不匹配")
    # JSON 字段包含 clues/flags 等嵌套容器；深拷贝后再整体写回，确保 ORM 能检测到变化，
    # 避免当前响应看到新状态、下一回合重新读取却丢失。
    state = copy.deepcopy(session.state_json)
    current_time = datetime.fromisoformat(state["world_time"])
    new_revision = session.state_version + 1
    command = envelope.command
    check: CheckRead | None = None
    pending: list[PendingDecision] = []
    event_type: str
    event_payload: dict[str, object]
    narration_facts: list[str]
    if command.kind == "wait_until":
        target_time = command.target_time
        if target_time <= current_time:
            raise GmRuntimeError("等待时间必须晚于当前世界时间")
        # 定时威胁以最近边界打断等待，避免把世界时间直接跳过事件。
        boundary = state.get("next_interrupt_at")
        interrupted = boundary and current_time < datetime.fromisoformat(boundary) < target_time
        effective_time = datetime.fromisoformat(boundary) if interrupted else target_time
        state["world_time"] = effective_time.isoformat()
        event_type = "time_advanced"
        event_payload = {
            "from": current_time.isoformat(),
            "to": effective_time.isoformat(),
            "interrupted": bool(interrupted),
        }
        narration_facts = [_format_time_change(current_time, effective_time)]
        if interrupted:
            state["next_interrupt_at"] = None
            if state.get("flags", {}).get("night_watch"):
                state["scene_id"] = "confrontation"
            elif state.get("flags", {}).get("window_watch"):
                state["scene_id"] = "chase"
    elif command.kind == "move_actor":
        exits = _LOCATIONS.get(actor.location_id, set())
        if command.target_id not in exits:
            raise GmRuntimeError("目标地点不是当前地点的合法出口")
        previous_location = actor.location_id
        actor.location_id = command.target_id
        state["location_id"] = command.target_id
        known_locations = state.setdefault("known_locations", [])
        if isinstance(known_locations, list) and command.target_id not in known_locations:
            known_locations.append(command.target_id)
        state["scene_id"] = _scene_for_location(state, command.target_id)
        event_type = "actor_moved"
        event_payload = {"from": previous_location, "to": command.target_id}
        narration_facts = [
            f"你从{_location_label(state, previous_location)}来到了"
            f"{_location_label(state, command.target_id)}。"
        ]
    elif command.kind == "inspect_target" or command.kind == "talk_to_npc":
        if not _target_is_available(state, actor, command.kind, command.target_id):
            raise GmRuntimeError("目标不在当前地点的可见对象中")
        event_type = "target_inspected" if command.kind == "inspect_target" else "npc_contacted"
        event_payload = {"target_id": command.target_id, "topic": getattr(command, "topic", "")}
        runtime_facts = _apply_runtime_action_outcome(state, actor, command.kind, command.target_id)
        if runtime_facts is None:
            narration_facts = [f"你完成了对 {command.target_id} 的行动。"]
            _apply_module_target(state, actor, command.target_id, getattr(command, "topic", ""))
        else:
            narration_facts = runtime_facts
    elif command.kind == "start_check":
        skill = _skill_definition(state, command.skill_id)
        if skill is None:
            raise GmRuntimeError("技能未在当前规则切片中实现")
        checkpoint = _checkpoint_definition(state, command.goal)
        if checkpoint is not None and not _checkpoint_is_available(state, checkpoint):
            raise GmRuntimeError("该检定在当前场景或时间不可用")
        if checkpoint is not None and checkpoint.get("skill") != command.skill_id:
            raise GmRuntimeError("检定技能与模组声明不一致")
        if await db.get(CheckRun, command.check_id) is not None:
            raise GmRuntimeError("check_id 已存在")
        target_value = _actor_check_value(actor, command.skill_id, int(skill["base"]))
        run = CheckRun(
            id=command.check_id,
            room_id=room_id,
            actor_id=actor.id,
            client_request_id=envelope.client_request_id,
            skill_id=command.skill_id,
            goal=command.goal,
            difficulty=command.difficulty,
            status="awaiting_roll",
            target_value=target_value,
        )
        decision = PendingDecisionRecord(
            id=str(uuid.uuid4()),
            room_id=room_id,
            actor_id=actor.id,
            check_id=command.check_id,
            kind="roll_check",
            options=["roll"],
            status="open",
        )
        db.add_all([run, decision])
        event_type = "check_started"
        event_payload = {
            "check_id": command.check_id,
            "skill_id": command.skill_id,
            "goal": command.goal,
        }
        narration_facts = [
            f"你准备进行「{_checkpoint_label(state, command.goal)}」的"
            f"{_skill_fact_label(state, command.skill_id)}检定。"
        ]
        pending = [_pending_read(decision)]
        check = _check_read(run, state)
    elif command.kind == "roll_check":
        run = await db.get(CheckRun, command.check_id, with_for_update=True)
        if run is None or run.room_id != room_id or run.actor_id != actor.id:
            raise GmRuntimeError("检定不存在")
        if run.status == "resolved":
            raise GmRuntimeError("检定已经结算，请重放原始 client_request_id 获取回执")
        if run.status != "awaiting_roll":
            raise GmRuntimeError("检定不在等待投骰状态")
        roll = secrets.randbelow(100) + 1
        multiplier = {"regular": 1, "hard": 0.5, "extreme": 0.2}[run.difficulty]
        target_value = max(1, int(run.target_value * multiplier))
        run.roll = roll
        run.target_value = target_value
        run.success = roll <= target_value
        run.status = "resolved"
        run.resolved_at = datetime.now(UTC)
        decision = await db.scalar(
            select(PendingDecisionRecord).where(PendingDecisionRecord.check_id == run.id)
        )
        if decision:
            decision.status = "resolved"
        check = _check_read(run, state)
        event_type = "check_resolved"
        event_payload = {
            "check_id": run.id,
            "roll": roll,
            "target_value": target_value,
            "success": run.success,
        }
        narration_facts = [
            f"{_skill_fact_label(state, run.skill_id)}检定"
            f"{'成功' if run.success else '失败'}，"
            f"骰点为 {roll}，目标值为 {target_value}。"
        ]
        narration_facts.extend(_apply_check_outcome(state, actor, run.goal, bool(run.success)))
    elif command.kind == "choose_option":
        runtime_facts = _apply_runtime_action_outcome(state, actor, command.kind, command.option_id)
        if runtime_facts is None:
            _apply_module_target(state, actor, command.option_id, command.option_id)
            narration_facts = []
        else:
            narration_facts = runtime_facts
        if state.get("ending_id") is None:
            raise GmRuntimeError("该剧情选择当前不可用")
        event_type = "ending_committed"
        event_payload = {"ending_id": state["ending_id"]}
        narration_facts = narration_facts or [f"结局已确定：{state['ending_id']}。"]
    else:
        raise GmRuntimeError("不支持的 Kernel 命令")
    session.state_json = state
    session.state_version = new_revision
    session.updated_at = datetime.now(UTC)
    event_id = str(uuid.uuid4())
    event = DomainEventEnvelope(
        event_id=event_id, event_type=event_type, actor_id=envelope.actor_id, payload=event_payload
    )
    db.add(
        GameEvent(
            id=event_id,
            room_id=room_id,
            sequence=new_revision,
            client_request_id=envelope.client_request_id,
            event_type=event.event_type,
            actor_id=event.actor_id,
            visibility=event.visibility,
            payload=event.payload,
        )
    )
    projection = _projection(session, actor).model_copy(
        update={
            "pending_decisions": pending,
            "checks": [check] if check is not None else [],
        }
    )
    result = CommandResult(
        client_request_id=envelope.client_request_id,
        revision=new_revision,
        events=[event],
        projection=projection,
        pending_decisions=pending,
        check=check,
        narration_facts=narration_facts,
    )
    result_json = result.model_dump(mode="json")
    receipt = CommandReceipt(
        room_id=room_id,
        client_request_id=envelope.client_request_id,
        revision=new_revision,
        result_json=result_json,
        created_at=datetime.now(UTC),
    )
    outbox = OutboxMessage(
        room_id=room_id,
        event_id=event_id,
        payload=result_json,
        created_at=datetime.now(UTC),
    )
    db.add_all([receipt, outbox])
    await db.commit()
    if narrate and command.kind == "roll_check":
        result = await _narrate_committed_result(
            db,
            room_id=room_id,
            actor_id=envelope.actor_id,
            result=result,
            receipt=receipt,
            outbox=outbox,
        )
    return result


async def _narrate_committed_result(
    db: AsyncSession,
    *,
    room_id: str,
    actor_id: str,
    result: CommandResult,
    receipt: CommandReceipt,
    outbox: OutboxMessage,
) -> CommandResult:
    """检定已提交后生成玩家可见续写，失败时保留确定性事实。"""

    snapshot = await build_context_snapshot(
        db, room_id=room_id, actor_id=actor_id, purpose="narration"
    )
    session = await db.get(GameSession, room_id)
    if session is None:
        return result
    _interpreter, narrator = _agents()
    narration: str | None = None
    # 只重试无副作用的叙事生成；权威检定、事件和回执已经提交，绝不重复执行。
    for _attempt in range(2):
        try:
            draft = await narrator.narrate(
                snapshot.model_copy(update={"action_candidates": []}),
                [event.event_id for event in result.events],
                result.narration_facts,
            )
            narration = guard_narration(
                draft,
                committed_event_ids=[event.event_id for event in result.events],
                visible_facts=[*snapshot.visible_facts, *result.narration_facts],
                forbidden_terms=[
                    *_forbidden_narration_terms(session.state_json),
                    *_time_narration_forbidden_terms(snapshot.world_time),
                ],
            ).text
            break
        except (GmModelUnavailable, ValueError):
            continue
    if narration is None:
        # 模型失败或被安全门禁拒绝时，仍用已提交的公开后果继续对话；
        # 不重新投骰，也不把未验证的模型文本发给玩家。
        public_outcome = result.narration_facts[1:]
        narration = (
            " ".join(public_outcome)
            if public_outcome
            else "这次检定已经结算，没有产生额外可确认的信息。"
        )

    enriched = result.model_copy(update={"narration": narration})
    payload = enriched.model_dump(mode="json")
    # 续写与命令回执一起保存，重放同一请求不再调用模型。
    receipt.result_json = payload
    outbox.payload = payload
    await db.commit()
    return enriched


def _pending_read(record: PendingDecisionRecord) -> PendingDecision:
    """把数据库待决策转换为不含内部字段的玩家 DTO。"""

    return PendingDecision(
        decision_id=record.id,
        kind="roll_check",
        check_id=record.check_id,
        options=list(record.options),
    )


def _check_read(run: CheckRun, state: dict[str, Any] | None = None) -> CheckRead:
    """把检定内部记录转换为玩家可见结果。"""

    return CheckRead(
        check_id=run.id,
        skill_id=run.skill_id,
        skill_label=_skill_label(state, run.skill_id) if state is not None else None,
        difficulty=cast(Literal["regular", "hard", "extreme"], run.difficulty),
        status=cast(Literal["awaiting_roll", "resolved"], run.status),
        roll=run.roll,
        target_value=run.target_value,
        success=run.success,
    )


def _session_read(session: GameSession, actor: RuntimeActor) -> SessionRead:
    """把 ORM 会话转换成玩家安全 DTO。"""

    runtime = _runtime_from_session(session)
    return SessionRead(
        session_id=session.room_id,
        module_id=session.module_id,
        module_version=session.module_version,
        projection=_projection(session, actor),
        opening_narration=runtime.get("opening_narration"),
    )


def _projection(session: GameSession, actor: RuntimeActor) -> PlayerProjection:
    """只选择玩家可见字段，避免把 keeper 状态扩散到 API。"""

    runtime = _runtime_from_session(session)
    scene_id = session.state_json.get("scene_id")
    scene = next((item for item in runtime.get("scenes", []) if item.get("id") == scene_id), {})
    labels = runtime.get("location_labels", {})
    known_locations = session.state_json.get("known_locations", [actor.location_id])
    return PlayerProjection(
        session_id=session.room_id,
        actor_id=actor.id,
        revision=session.state_version,
        world_time=datetime.fromisoformat(session.state_json["world_time"]),
        location_id=actor.location_id,
        visible_facts=list(session.state_json.get("visible_facts", [])),
        scene_id=scene_id,
        scene_label=scene.get("label"),
        known_locations=[
            KnownLocationRead(
                id=str(location_id),
                label=(
                    str(labels.get(location_id, location_id))
                    if isinstance(labels, dict)
                    else str(location_id)
                ),
            )
            for location_id in known_locations
            if isinstance(location_id, str)
        ],
        clues=list(session.state_json.get("clues", [])),
        hp=actor.state_json.get("hp"),
        san=actor.state_json.get("san"),
        ending_id=session.state_json.get("ending_id"),
    )


def _runtime_from_session(session: GameSession) -> dict[str, Any]:
    """读取会话冻结的结构化运行包，避免运行中再次读取原始模组文件。"""

    runtime = session.state_json.get("_runtime", {})
    return cast(dict[str, Any], runtime) if isinstance(runtime, dict) else {}


def _forbidden_narration_terms(state: dict[str, Any]) -> list[str]:
    """返回当前尚未满足披露条件的模组词，阻止模型提前剧透。"""

    runtime = state.get("_runtime", {})
    if not isinstance(runtime, dict):
        return []
    owned_clues = set(state.get("clues", []))
    return [
        str(guard["term"])
        for guard in runtime.get("disclosure_guards", [])
        if isinstance(guard, dict)
        and isinstance(guard.get("term"), str)
        and not owned_clues.intersection(guard.get("requires_any_clues", []))
    ]


def _time_narration_forbidden_terms(world_time: datetime) -> tuple[str, ...]:
    """按权威当地小时禁止明显冲突的时段描述。"""

    if world_time.hour >= 20 or world_time.hour < 6:
        return ("黄昏", "暮色", "傍晚", "夕阳", "午后", "上午", "阳光")
    if 17 <= world_time.hour < 20:
        return ("午后", "上午", "正午", "深夜", "月光")
    if 12 <= world_time.hour < 17:
        return ("黄昏", "暮色", "傍晚", "夕阳", "上午", "夜幕", "深夜", "月光")
    return ("黄昏", "暮色", "傍晚", "夕阳", "午后", "夜幕", "深夜", "月光")


def _skill_definition(state: dict[str, Any], skill_id: str) -> dict[str, Any] | None:
    """优先读取会话冻结的模组技能，兼容旧测试仍使用的内置规则切片。"""

    runtime = state.get("_runtime", {})
    if isinstance(runtime, dict):
        for skill in runtime.get("skills", []):
            if isinstance(skill, dict) and skill.get("id") == skill_id:
                return skill
    return _SKILLS.get(skill_id)


def _location_label(state: dict[str, Any], location_id: str) -> str:
    """读取冻结模组中的玩家可见地点名称。"""

    runtime = state.get("_runtime", {})
    labels = runtime.get("location_labels", {}) if isinstance(runtime, dict) else {}
    return str(labels.get(location_id, location_id)) if isinstance(labels, dict) else location_id


def _format_world_time(value: datetime) -> str:
    """把权威时间转换为面向玩家的简洁中文格式，不暴露存储用 ISO 字符串。"""

    return f"{value.month}月{value.day}日{value.hour:02d}:{value.minute:02d}"


def _format_time_change(start: datetime, end: datetime) -> str:
    """描述时间推进；同一天不重复日期，跨日时保留两端日期。"""

    end_text = (
        f"{end.hour:02d}:{end.minute:02d}"
        if start.date() == end.date()
        else _format_world_time(end)
    )
    return f"时间从{_format_world_time(start)}推进到{end_text}。"


def _skill_label(state: dict[str, Any], skill_id: str) -> str:
    """把稳定技能 ID 转换为玩家可读名称。"""

    skill = _skill_definition(state, skill_id)
    return str(skill.get("name", skill_id)) if skill is not None else skill_id


def _skill_fact_label(state: dict[str, Any], skill_id: str) -> str:
    """为 Narrator 补充模组声明的技能语义，避免只按名称臆测。"""

    skill = _skill_definition(state, skill_id)
    label = _skill_label(state, skill_id)
    purpose = skill.get("purpose") if skill is not None else None
    return f"{label}（{purpose}）" if isinstance(purpose, str) and purpose else label


def _actor_check_value(actor: RuntimeActor, skill_id: str, fallback: int) -> int:
    """从角色权威状态读取检定值，特殊属性不混入普通技能字典。"""

    if skill_id == "luck":
        return int(actor.state_json.get("luck", fallback))
    if skill_id == "sanity":
        return int(actor.state_json.get("san", fallback))
    skills = actor.state_json.get("skills", {})
    return int(skills.get(skill_id, fallback)) if isinstance(skills, dict) else fallback


def _target_is_available(
    state: dict[str, Any], actor: RuntimeActor, action: str, target_id: str
) -> bool:
    """同时兼容旧内置对象和运行包声明的当前场景动作。"""

    if target_id in _TARGETS.get(actor.location_id, set()):
        return True
    runtime = state.get("_runtime", {})
    if not isinstance(runtime, dict):
        return False
    owned_clues = set(state.get("clues", []))
    return any(
        isinstance(item, dict)
        and item.get("scene_id") == state.get("scene_id")
        and item.get("action") == action
        and item.get("target_id") == target_id
        and set(item.get("requires_clues", [])) <= owned_clues
        for item in runtime.get("actions", [])
    )


def _apply_runtime_action_outcome(
    state: dict[str, Any], actor: RuntimeActor, action: str, target_id: str
) -> list[str] | None:
    """应用运行包声明的普通动作后果，不从目标名称推断剧情。"""

    runtime = state.get("_runtime", {})
    if not isinstance(runtime, dict):
        return None
    owned_clues = set(state.get("clues", []))
    definition = next(
        (
            item
            for item in runtime.get("actions", [])
            if isinstance(item, dict)
            and item.get("scene_id") == state.get("scene_id")
            and item.get("action") == action
            and item.get("target_id") == target_id
            and set(item.get("requires_clues", [])) <= owned_clues
        ),
        None,
    )
    if definition is None:
        return None
    outcome = definition.get("outcome", {})
    if not isinstance(outcome, dict):
        return []
    _add_clues(state, [str(clue) for clue in outcome.get("clues", [])])
    scene_id = outcome.get("scene_id")
    if isinstance(scene_id, str):
        state["scene_id"] = scene_id
    location_id = outcome.get("location_id")
    if isinstance(location_id, str):
        actor.location_id = location_id
        state["location_id"] = location_id
        known_locations = state.setdefault("known_locations", [])
        if isinstance(known_locations, list) and location_id not in known_locations:
            known_locations.append(location_id)
    ending_id = outcome.get("ending_id")
    if isinstance(ending_id, str):
        state["ending_id"] = ending_id
    return [str(fact) for fact in outcome.get("facts", [])]


def _checkpoint_label(state: dict[str, Any], checkpoint_id: str) -> str:
    """把检定节点 ID 转换为模组声明的玩家可读目标。"""

    runtime = state.get("_runtime", {})
    if isinstance(runtime, dict):
        for checkpoint in runtime.get("checkpoints", []):
            if isinstance(checkpoint, dict) and checkpoint.get("id") == checkpoint_id:
                return str(checkpoint.get("label", checkpoint_id))
    return checkpoint_id


def _checkpoint_definition(state: dict[str, Any], checkpoint_id: str) -> dict[str, Any] | None:
    """读取会话冻结的检定声明。"""

    runtime = state.get("_runtime", {})
    if isinstance(runtime, dict):
        for checkpoint in runtime.get("checkpoints", []):
            if isinstance(checkpoint, dict) and checkpoint.get("id") == checkpoint_id:
                return cast(dict[str, Any], checkpoint)
    return None


def _checkpoint_is_available(state: dict[str, Any], checkpoint: dict[str, Any]) -> bool:
    """在 Engine 内重新校验场景、线索与时段，不信任模型候选。"""

    if checkpoint.get("scene_id") != state.get("scene_id"):
        return False
    if not set(checkpoint.get("requires_clues", [])) <= set(state.get("clues", [])):
        return False
    window = checkpoint.get("available_hours")
    if not isinstance(window, dict):
        return True
    start = window.get("start")
    end = window.get("end")
    if not isinstance(start, int) or not isinstance(end, int):
        return False
    hour = datetime.fromisoformat(str(state["world_time"])).hour
    return start <= hour < end if start < end else hour >= start or hour < end


def _scene_for_location(state: dict[str, Any], location_id: str) -> str:
    """使用会话冻结运行包映射地点，避免切换模组时修改主持代码。"""

    runtime = state.get("_runtime", {})
    if isinstance(runtime, dict):
        route_rules = runtime.get("route_rules", {})
        move_to = route_rules.get("move_to", {}) if isinstance(route_rules, dict) else {}
        if isinstance(move_to, dict) and isinstance(move_to.get(location_id), str):
            return str(move_to[location_id])
        for scene in runtime.get("scenes", []):
            if isinstance(scene, dict) and scene.get("location_id") == location_id:
                return str(scene.get("id", state.get("scene_id", "briefing")))
    return str(state.get("scene_id", "briefing"))


def _add_clues(state: dict[str, Any], clues: list[str]) -> None:
    """幂等地授予公开线索，避免重试重复写入。"""

    owned_value = state.setdefault("clues", [])
    if not isinstance(owned_value, list):
        owned = []
        state["clues"] = owned
    else:
        owned = cast(list[str], owned_value)
    for clue in clues:
        if clue not in owned:
            owned.append(clue)


def _apply_module_target(
    state: dict[str, Any], actor: RuntimeActor, target_id: str, topic: str
) -> None:
    """按《追书人》运行包的公开动作规则推进线索和路线标记。"""

    flags_value = state.setdefault("flags", {})
    if not isinstance(flags_value, dict):
        flags = {}
        state["flags"] = flags
    else:
        flags = cast(dict[str, Any], flags_value)
    if target_id in {"neighbors", "ask_neighbors"}:
        state["scene_id"] = "neighbors"
        _add_clues(state, ["douglas_cemetery"])
    elif target_id in {"library", "library_research"}:
        state["scene_id"] = "library"
        _add_clues(state, ["old_report"])
    elif target_id in {"newspaper", "newspaper_archive"}:
        state["scene_id"] = "newspaper"
        _add_clues(state, ["hilda_statement"])
    elif target_id in {"search_study", "read_diary"}:
        state["scene_id"] = "kimball_house"
        _add_clues(state, ["diary_choice", "tunnel_hint"])
    elif target_id in {"gravekeeper", "track_grave", "night_watch"}:
        state["scene_id"] = "cemetery"
        _add_clues(state, ["favorite_grave", "night_silhouette"])
        if target_id == "night_watch":
            state["flags"]["night_watch"] = True
            state["next_interrupt_at"] = (
                datetime.fromisoformat(str(state["world_time"])) + timedelta(hours=1)
            ).isoformat()
    elif target_id in {"surveillance", "lock_window"}:
        state["scene_id"] = "kimball_house"
        flags["window_watch"] = True
        state["next_interrupt_at"] = (
            datetime.fromisoformat(str(state["world_time"])) + timedelta(hours=1)
        ).isoformat()
    elif target_id == "chase_thief":
        actor.location_id = "cemetery"
        state["scene_id"] = "cemetery"
        flags["window_watch"] = False
        _add_clues(state, ["tunnel_hint"])
    elif target_id in {"call_douglas", "open_crypt", "follow_douglas"}:
        state["scene_id"] = "douglas" if target_id != "open_crypt" else "crypt"
        _add_clues(state, ["tunnel_hint"])
    elif target_id in {"talk_douglas", "douglas"} or "礼貌" in topic:
        state["scene_id"] = "douglas"
        _add_clues(state, ["douglas_truth", "ghouls_leaving"])
        flags["douglas_conversation_completed"] = True
        state["ending_id"] = "peaceful_resolution"
    elif target_id == "peaceful_resolution":
        state["ending_id"] = "peaceful_resolution"
    elif target_id == "follow_underground":
        state["ending_id"] = "follow_underground"
    elif target_id == "attack_douglas":
        flags["douglas_alive"] = False
        flags["ghouls_active"] = True
    elif target_id == "fight_ghouls":
        flags["ghouls_active"] = False
        state["ending_id"] = state.get("ending_id") or "douglas_killed"
    elif target_id == "flee" or (target_id == "leave" and flags.get("douglas_alive") is False):
        state["ending_id"] = "flee"


def _apply_check_outcome(
    state: dict[str, Any], actor: RuntimeActor, goal: str, success: bool
) -> list[str]:
    """按模组 checkpoint 应用检定结果；旧自由目标只保留兼容行为。"""

    runtime = state.get("_runtime", {})
    if isinstance(runtime, dict):
        checkpoint = next(
            (
                item
                for item in runtime.get("checkpoints", [])
                if isinstance(item, dict) and item.get("id") == goal
            ),
            None,
        )
        if checkpoint is not None:
            outcome_key = "success_outcome" if success else "failure_outcome"
            outcome = checkpoint.get(outcome_key, {})
            outcome = outcome if isinstance(outcome, dict) else {}
            clue_key = "success_clues" if success else "failure_clues"
            clue_ids = [str(clue) for clue in outcome.get("clues", checkpoint.get(clue_key, []))]
            _add_clues(state, clue_ids)
            # 场景和时间后果由模组声明，避免 Kernel 认识任何具体剧情节点。
            scene_id = outcome.get("scene_id")
            if isinstance(scene_id, str) and scene_id:
                state["scene_id"] = scene_id
            advance_minutes = outcome.get("advance_minutes", 0)
            if isinstance(advance_minutes, int) and advance_minutes > 0:
                current_time = datetime.fromisoformat(str(state["world_time"]))
                advanced_time = current_time + timedelta(minutes=advance_minutes)
                state["world_time"] = advanced_time.isoformat()
            public_clues = {
                str(clue.get("id")): str(clue.get("text"))
                for clue in runtime.get("clues", [])
                if isinstance(clue, dict)
                and clue.get("visibility") == "public"
                and isinstance(clue.get("id"), str)
                and isinstance(clue.get("text"), str)
            }
            facts = [str(fact) for fact in outcome.get("facts", []) if isinstance(fact, str)]
            return facts or [
                public_clues[clue_id] for clue_id in clue_ids if clue_id in public_clues
            ]

    lowered = goal.lower()
    if any(word in goal for word in ("邻居", "朋友")):
        _add_clues(state, ["douglas_cemetery"])
    elif "图书馆" in goal or "报纸" in goal:
        _add_clues(state, ["old_report"] if success else ["old_report"])
    elif "档案" in goal or "证词" in goal:
        _add_clues(state, ["hilda_statement"])
    elif "书房" in goal or "日记" in goal:
        _add_clues(state, ["diary_choice", "tunnel_hint"])
    elif "墓" in goal or "足迹" in goal or "监视" in goal:
        _add_clues(state, ["tunnel_hint", "night_silhouette"])
    elif "理智" in goal or "食尸鬼" in lowered:
        actor.state_json["san"] = max(
            0, int(actor.state_json.get("san", 50)) - (1 if not success else 0)
        )
        if "食尸鬼" in goal and not success:
            state["ending_id"] = "asylum"
    return []


async def submit_free_text(
    db: AsyncSession,
    *,
    room_id: str,
    payload: TurnInputBody,
) -> GmTurnRead:
    """解释一回合自然语言并把首个合法动作交给 Kernel。"""

    previous = await db.scalar(
        select(TurnRun).where(TurnRun.client_request_id == payload.client_request_id)
    )
    if previous is not None and previous.room_id != room_id:
        raise GmRuntimeError("重复请求 ID 已属于其他房间")
    if previous is not None and previous.result_json is not None and previous.status != "failed":
        return GmTurnRead.model_validate(previous.result_json)
    turn = previous or TurnRun(
        room_id=room_id,
        client_request_id=payload.client_request_id,
        status="understanding",
        expected_revision=payload.expected_revision,
        actor_id=payload.actor_id,
        input_text=payload.input,
    )
    if previous is None:
        db.add(turn)
        await db.commit()
    else:
        # 失败回合只恢复模型调用，不重放已经提交的 Kernel 命令。
        turn.status = "understanding"
        turn.result_json = None
        turn.expected_revision = payload.expected_revision
        turn.actor_id = payload.actor_id
        turn.input_text = payload.input
        await db.commit()
    try:
        open_decision = await db.scalar(
            select(PendingDecisionRecord).where(
                PendingDecisionRecord.room_id == room_id,
                PendingDecisionRecord.actor_id == payload.actor_id,
                PendingDecisionRecord.status == "open",
            )
        )
        if open_decision is not None:
            raise GmRuntimeError("请先完成当前待投骰检定")
        snapshot = await build_context_snapshot(db, room_id=room_id, actor_id=payload.actor_id)
        # 保存模型实际看到的安全快照，便于复现原文片段选择和裁剪结果。
        turn.context_json = snapshot.model_dump(mode="json")
        await db.commit()
        interpreter, narrator = _agents()
        intent = validate_intent(snapshot, await interpreter.interpret(snapshot, payload.input))
        intent = guard_intent_coverage(snapshot, intent, payload.input)
        session = await db.get(GameSession, room_id)
        runtime = session.state_json.get("_runtime", {}) if session is not None else {}
        location_labels = runtime.get("location_labels", {}) if isinstance(runtime, dict) else {}
        intent = guard_move_target(snapshot, intent, payload.input, location_labels)
        if intent.kind == "clarification":
            hidden_terms = (
                _forbidden_narration_terms(session.state_json) if session is not None else []
            )
            # 澄清同样直接面向玩家；除未解锁剧情外，也禁止回显模型看到的内部目标 ID。
            intent = guard_clarification(
                intent,
                forbidden_terms=[
                    *hidden_terms,
                    *(candidate.target_id or "" for candidate in snapshot.action_candidates),
                ],
            )
            result = GmTurnRead(
                client_request_id=payload.client_request_id,
                status="clarification",
                revision=snapshot.revision,
                clarification_question=intent.clarification_question,
                clarification_options=intent.clarification_options,
            )
            turn.status = "awaiting_clarification"
            turn.result_json = result.model_dump(mode="json")
            await db.commit()
            return result
        command = intent_step_to_command(
            intent.steps[0],
            client_request_id=payload.client_request_id,
            expected_revision=payload.expected_revision,
            actor_id=payload.actor_id,
        )
        command_result = await submit_command(db, room_id=room_id, envelope=command)
        narration: str | None = None
        try:
            refreshed = await build_context_snapshot(
                db, room_id=room_id, actor_id=payload.actor_id, purpose="narration"
            )
            refreshed_session = await db.get(GameSession, room_id)
            forbidden_terms = (
                _forbidden_narration_terms(refreshed_session.state_json)
                if refreshed_session is not None
                else []
            )
            forbidden_terms.extend(_time_narration_forbidden_terms(refreshed.world_time))
            # Narrator 只表达本回合已提交事实；未来动作候选属于意图解释器输入，
            # 不能让叙事模型误当成当前场景中已经出现的人物或线索。
            narration_snapshot = refreshed.model_copy(update={"action_candidates": []})
            draft = await narrator.narrate(
                narration_snapshot,
                [event.event_id for event in command_result.events],
                command_result.narration_facts,
            )
            narration = guard_narration(
                draft,
                committed_event_ids=[event.event_id for event in command_result.events],
                visible_facts=[*refreshed.visible_facts, *command_result.narration_facts],
                forbidden_terms=forbidden_terms,
            ).text
        except (GmModelUnavailable, ValueError):
            # Kernel 已提交时不能回滚；模型越界时只展示已验证的中文事实。
            narration = " ".join(command_result.narration_facts)
        result = GmTurnRead(
            client_request_id=payload.client_request_id,
            status="completed",
            revision=command_result.revision,
            narration=narration,
            command_result=command_result,
        )
        turn.status = "completed"
        turn.result_json = result.model_dump(mode="json")
        await db.commit()
        return result
    except (GmModelUnavailable, ValueError, GmRuntimeError) as exc:
        result = GmTurnRead(
            client_request_id=payload.client_request_id,
            status="failed",
            revision=payload.expected_revision,
        )
        turn.status = "failed"
        turn.result_json = result.model_dump(mode="json")
        await db.commit()
        message = "gm_unavailable" if isinstance(exc, GmModelUnavailable) else str(exc)
        raise GmRuntimeError(message) from exc


async def read_projection(
    db: AsyncSession,
    *,
    room_id: str,
    actor_id: str,
) -> PlayerProjection:
    """读取重连所需的当前玩家投影，不触发模型或规则执行。"""

    session = await db.get(GameSession, room_id)
    actor = await db.get(RuntimeActor, actor_id)
    if session is None or actor is None or actor.room_id != room_id:
        raise GmRuntimeError("GM 会话或调查员不存在")
    decisions = list(
        await db.scalars(
            select(PendingDecisionRecord).where(
                PendingDecisionRecord.room_id == room_id,
                PendingDecisionRecord.actor_id == actor_id,
                PendingDecisionRecord.status == "open",
            )
        )
    )
    checks: list[CheckRun] = []
    for decision in decisions:
        run = await db.get(CheckRun, decision.check_id)
        if run is not None:
            checks.append(run)
    latest_turn = await db.scalar(
        select(TurnRun)
        .where(
            TurnRun.room_id == room_id,
            TurnRun.actor_id == actor_id,
        )
        .order_by(TurnRun.created_at.desc())
        .limit(1)
    )
    clarification: ClarificationRead | None = None
    # 澄清不是长期任务；玩家提交新行动后，旧问题不得在刷新时重新挂起。
    if (
        latest_turn is not None
        and latest_turn.status == "awaiting_clarification"
        and latest_turn.result_json is not None
    ):
        turn_result = GmTurnRead.model_validate(latest_turn.result_json)
        if turn_result.clarification_question:
            clarification = ClarificationRead(
                client_request_id=turn_result.client_request_id,
                question=turn_result.clarification_question,
                options=turn_result.clarification_options,
            )
    return _projection(session, actor).model_copy(
        update={
            "pending_decisions": [_pending_read(decision) for decision in decisions],
            "checks": [_check_read(run, session.state_json) for run in checks],
            "pending_clarification": clarification,
        }
    )

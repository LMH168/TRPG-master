"""Phase 0 GM Kernel 的会话安装、Wait 命令和幂等回执服务。"""

import copy
import re
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
    EncounterRead,
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
    propose_adjudication,
    validate_adjudication,
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
            "scheduled_events": [],
            "npc_states": {
                str(npc["id"]): {
                    "alive": True,
                    "hp": int(npc.get("stats", {}).get("hp", 1)),
                    "max_hp": int(npc.get("stats", {}).get("hp", 1)),
                }
                for npc in runtime.get("npcs", [])
                if isinstance(npc, dict)
                and isinstance(npc.get("id"), str)
                and isinstance(npc.get("stats"), dict)
            },
            # 运行包随会话冻结在数据库中；投影函数只挑选公开字段，不会返回此私有定义。
            "_runtime": runtime,
        },
        state_version=0,
        created_at=now,
        updated_at=now,
    )
    actor_hp = int(derived.get("HP", initial.get("hp", 10)))
    actor = RuntimeActor(
        id=actor_id,
        room_id=room_id,
        display_name=(character.name or display_name) if character is not None else display_name,
        location_id=initial_location,
        state_json={
            "alive": True,
            "hp": actor_hp,
            "max_hp": actor_hp,
            "san": int(derived.get("SAN", initial.get("san", 50))),
            "luck": int(attributes.get("LUCK", 50)),
            "skills": dict(skills),
            "attributes": dict(attributes),
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
        scheduled = _next_scheduled_event(state, target_time)
        effective_time = (
            datetime.fromisoformat(str(scheduled["due_at"]))
            if scheduled is not None
            else target_time
        )
        state["world_time"] = effective_time.isoformat()
        event_type = "time_advanced"
        event_payload = {
            "from": current_time.isoformat(),
            "to": effective_time.isoformat(),
            "interrupted": scheduled is not None,
        }
        narration_facts = [_format_time_change(current_time, effective_time)]
        if scheduled is not None:
            narration_facts.extend(_apply_scheduled_event(state, actor, scheduled))
    elif command.kind == "move_actor":
        location = _runtime_definition(state, "locations", actor.location_id)
        exits = set(location.get("exits", [])) if location is not None else set()
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
            raise GmRuntimeError("模组没有声明该动作的权威效果")
        narration_facts = runtime_facts
    elif command.kind == "start_check":
        skill = _skill_definition(state, command.skill_id)
        if skill is None:
            raise GmRuntimeError("技能未在当前规则切片中实现")
        checkpoint = _checkpoint_for_request(state, command.goal, command.skill_id)
        if checkpoint is None:
            raise GmRuntimeError("该检定未在当前场景声明")
        if not _checkpoint_is_available(state, checkpoint):
            raise GmRuntimeError("该检定在当前场景或时间不可用")
        if checkpoint.get("skill") != command.skill_id:
            raise GmRuntimeError("检定技能与模组声明不一致")
        expected_difficulty = str(checkpoint.get("difficulty", "regular"))
        if command.difficulty != expected_difficulty:
            raise GmRuntimeError("检定难度与模组声明不一致")
        if await db.get(CheckRun, command.check_id) is not None:
            raise GmRuntimeError("check_id 已存在")
        target_value = _actor_check_value(actor, command.skill_id, int(skill["base"]))
        run = CheckRun(
            id=command.check_id,
            room_id=room_id,
            actor_id=actor.id,
            client_request_id=envelope.client_request_id,
            skill_id=command.skill_id,
            goal=str(checkpoint["id"]),
            difficulty=command.difficulty,
            status="awaiting_roll",
            target_value=target_value,
            details_json={
                "bonus_dice": int(checkpoint.get("bonus_dice", 0)),
                "allow_luck": bool(checkpoint.get("allow_luck", False)),
                "allow_push": bool(checkpoint.get("allow_push", False)),
                "roll_values": [],
                "luck_spent": 0,
                "pushed": False,
                "outcome_applied": False,
            },
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
        details = copy.deepcopy(run.details_json or {})
        bonus_dice = int(details.get("bonus_dice", 0))
        roll, roll_values = _roll_d100(bonus_dice)
        level = _success_level(roll, run.target_value)
        success = _passes_difficulty(level, run.difficulty)
        run.roll = roll
        run.success = success
        details["roll_values"] = roll_values
        details["success_level"] = level
        details["final_result"] = success if success else None
        run.details_json = details
        decision = await db.scalar(
            select(PendingDecisionRecord).where(PendingDecisionRecord.check_id == run.id)
        )
        options = _post_roll_options(run, actor)
        if success or not options:
            run.status = "resolved"
            run.resolved_at = datetime.now(UTC)
            details["final_result"] = success
            details["outcome_applied"] = True
            run.details_json = details
            if decision:
                decision.status = "resolved"
            narration_facts = _check_roll_facts(state, run)
            narration_facts.extend(_apply_check_outcome(state, actor, run.goal, success))
            event_type = "check_resolved"
        else:
            run.status = "awaiting_roll_decision"
            if decision is None:
                raise GmRuntimeError("检定待决策记录不存在")
            decision.kind = "roll_decision"
            decision.options = options
            pending = [_pending_read(decision)]
            narration_facts = _check_roll_facts(state, run)
            event_type = "check_rolled"
        check = _check_read(run, state)
        event_payload = {
            "check_id": run.id,
            "roll": roll,
            "target_value": run.target_value,
            "success": success,
            "success_level": level,
        }
    elif command.kind == "resolve_check":
        run = await db.get(CheckRun, command.check_id, with_for_update=True)
        if run is None or run.room_id != room_id or run.actor_id != actor.id:
            raise GmRuntimeError("检定不存在")
        if run.status != "awaiting_roll_decision":
            raise GmRuntimeError("检定不在等待骰后选择状态")
        decision = await db.scalar(
            select(PendingDecisionRecord).where(PendingDecisionRecord.check_id == run.id)
        )
        if decision is None or command.option not in decision.options:
            raise GmRuntimeError("该骰后选择当前不可用")
        details = copy.deepcopy(run.details_json or {})
        if command.option == "spend_luck":
            cost = _luck_cost(run)
            luck = int(actor.state_json.get("luck", 0))
            if cost <= 0 or cost > luck:
                raise GmRuntimeError("幸运不足或该结果不能花费幸运")
            actor.state_json["luck"] = luck - cost
            details["luck_spent"] = cost
            details["success_level"] = _required_success_level(run.difficulty)
            run.success = True
        elif command.option == "push":
            if not command.revised_method or not command.revised_method.strip():
                raise GmRuntimeError("强推必须说明改变后的做法")
            pushed_roll, pushed_values = _roll_d100(int(details.get("bonus_dice", 0)))
            run.roll = pushed_roll
            details["roll_values"] = [*details.get("roll_values", []), *pushed_values]
            details["success_level"] = _success_level(pushed_roll, run.target_value)
            details["pushed"] = True
            details["revised_method"] = command.revised_method.strip()
            run.success = _passes_difficulty(str(details["success_level"]), run.difficulty)
        else:
            run.success = False
        run.status = "resolved"
        run.resolved_at = datetime.now(UTC)
        details["final_result"] = bool(run.success)
        details["outcome_applied"] = True
        run.details_json = details
        decision.status = "resolved"
        check = _check_read(run, state)
        event_type = "check_resolved"
        event_payload = {
            "check_id": run.id,
            "roll": run.roll,
            "target_value": run.target_value,
            "success": bool(run.success),
            "success_level": details.get("success_level"),
            "option": command.option,
        }
        narration_facts = _check_roll_facts(state, run)
        narration_facts.extend(_apply_check_outcome(state, actor, run.goal, bool(run.success)))
    elif command.kind == "choose_option":
        runtime_facts = _apply_runtime_action_outcome(state, actor, command.kind, command.option_id)
        if runtime_facts is None:
            raise GmRuntimeError("模组没有声明该剧情选择")
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
        kind=cast(Literal["roll_check", "roll_decision"], record.kind),
        check_id=record.check_id,
        options=list(record.options),
    )


def _check_read(run: CheckRun, state: dict[str, Any] | None = None) -> CheckRead:
    """把检定内部记录转换为玩家可见结果。"""

    details = run.details_json or {}
    return CheckRead(
        check_id=run.id,
        skill_id=run.skill_id,
        skill_label=_skill_label(state, run.skill_id) if state is not None else None,
        difficulty=cast(Literal["regular", "hard", "extreme"], run.difficulty),
        status=cast(Literal["awaiting_roll", "awaiting_roll_decision", "resolved"], run.status),
        roll=run.roll,
        target_value=run.target_value,
        success=run.success,
        success_level=details.get("success_level"),
        bonus_dice=int(details.get("bonus_dice", 0)),
        roll_values=[int(value) for value in details.get("roll_values", [])],
        luck_spent=int(details.get("luck_spent", 0)),
        pushed=bool(details.get("pushed", False)),
        final_result=details.get("final_result"),
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
        luck=actor.state_json.get("luck"),
        major_wound=bool(actor.state_json.get("major_wound", False)),
        unconscious=bool(actor.state_json.get("unconscious", False)),
        temporary_insanity=bool(actor.state_json.get("temporary_insanity", False)),
        encounter=_encounter_read(session.state_json.get("encounter")),
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
    """从会话冻结的规则切片读取技能定义。"""

    runtime = state.get("_runtime", {})
    if isinstance(runtime, dict):
        for skill in runtime.get("skills", []):
            if isinstance(skill, dict) and skill.get("id") == skill_id:
                return skill
    return None


def _runtime_definition(
    state: dict[str, Any], collection: str, object_id: str
) -> dict[str, Any] | None:
    """按稳定 ID 读取冻结运行包对象，Kernel 不识别任何模组专名。"""

    runtime = state.get("_runtime", {})
    if not isinstance(runtime, dict):
        return None
    return next(
        (
            cast(dict[str, Any], item)
            for item in runtime.get(collection, [])
            if isinstance(item, dict) and item.get("id") == object_id
        ),
        None,
    )


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
    attribute_ids = {"strength": "STR", "constitution": "CON", "dexterity": "DEX"}
    attributes = actor.state_json.get("attributes", {})
    attribute_id = attribute_ids.get(skill_id)
    if attribute_id and isinstance(attributes, dict):
        return int(attributes.get(attribute_id, fallback))
    skills = actor.state_json.get("skills", {})
    return int(skills.get(skill_id, fallback)) if isinstance(skills, dict) else fallback


def _target_is_available(
    state: dict[str, Any], actor: RuntimeActor, action: str, target_id: str
) -> bool:
    """校验动作声明、当前场景、前置线索和目标存活状态。"""

    runtime = state.get("_runtime", {})
    if not isinstance(runtime, dict):
        return False
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
        return _visible_runtime_target(state, actor, action, target_id)
    npc_id = definition.get("npc_id")
    npc_states = state.get("npc_states", {})
    return not (
        isinstance(npc_id, str)
        and isinstance(npc_states, dict)
        and npc_states.get(npc_id, {}).get("alive") is False
    )


def _visible_runtime_target(
    state: dict[str, Any], actor: RuntimeActor, action: str, target_id: str
) -> bool:
    """允许对当前地点公开对象或存活 NPC 提出无副作用的开放行动。"""

    collection = "npcs" if action == "talk_to_npc" else "objects"
    target = _runtime_definition(state, collection, target_id)
    if target is None or target.get("location_id") != actor.location_id:
        return False
    if not set(target.get("requires_clues", [])) <= set(state.get("clues", [])):
        return False
    if collection == "npcs":
        npc_states = state.get("npc_states", {})
        return not (
            isinstance(npc_states, dict) and npc_states.get(target_id, {}).get("alive") is False
        )
    return True


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
        if _visible_runtime_target(state, actor, action, target_id):
            label = _runtime_definition(
                state, "npcs" if action == "talk_to_npc" else "objects", target_id
            )
            display = (label or {}).get("name") or (label or {}).get("label") or target_id
            return [f"你完成了对{display}的行动；没有产生额外的权威状态变化。"]
        return None
    outcome = definition.get("outcome", {})
    if not isinstance(outcome, dict):
        return []
    return _apply_effects(state, actor, outcome)


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


def _checkpoint_for_request(
    state: dict[str, Any], goal: str, skill_id: str
) -> dict[str, Any] | None:
    """按 ID 优先解析检定；同场景同技能唯一时兼容玩家可读目标。"""

    direct = _checkpoint_definition(state, goal)
    if direct is not None:
        return direct
    runtime = state.get("_runtime", {})
    if not isinstance(runtime, dict):
        return None
    matches = [
        item
        for item in runtime.get("checkpoints", [])
        if isinstance(item, dict)
        and item.get("scene_id") == state.get("scene_id")
        and item.get("skill") == skill_id
        and _checkpoint_is_available(state, item)
    ]
    return cast(dict[str, Any], matches[0]) if len(matches) == 1 else None


def _checkpoint_is_available(state: dict[str, Any], checkpoint: dict[str, Any]) -> bool:
    """在 Engine 内重新校验场景、线索与时段，不信任模型候选。"""

    if checkpoint.get("scene_id") != state.get("scene_id"):
        return False
    if not set(checkpoint.get("requires_clues", [])) <= set(state.get("clues", [])):
        return False
    npc_id = checkpoint.get("npc_id")
    npc_states = state.get("npc_states", {})
    if (
        isinstance(npc_id, str)
        and isinstance(npc_states, dict)
        and npc_states.get(npc_id, {}).get("alive") is False
    ):
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


def _roll_d100(bonus_dice: int = 0) -> tuple[int, list[int]]:
    """由服务端生成 D100；奖惩骰共享个位并选择最低或最高结果。"""

    if bonus_dice == 0:
        roll = secrets.randbelow(100) + 1
        return roll, [roll]
    ones = secrets.randbelow(10)
    rolls = []
    for _index in range(abs(bonus_dice) + 1):
        tens = secrets.randbelow(10)
        value = tens * 10 + ones
        rolls.append(100 if value == 0 else value)
    return (min(rolls) if bonus_dice > 0 else max(rolls)), rolls


def _success_level(roll: int, target: int) -> str:
    """按 CoC7 阈值计算大成功、极难、困难、常规、失败或大失败。"""

    if roll == 1:
        return "critical"
    if roll == 100 or (target < 50 and roll >= 96):
        return "fumble"
    if roll <= max(1, target // 5):
        return "extreme"
    if roll <= max(1, target // 2):
        return "hard"
    if roll <= target:
        return "regular"
    return "failure"


def _passes_difficulty(level: str, difficulty: str) -> bool:
    """判断成功等级是否满足当前检定难度。"""

    rank = {"fumble": 0, "failure": 0, "regular": 1, "hard": 2, "extreme": 3, "critical": 4}
    required = {"regular": 1, "hard": 2, "extreme": 3}
    return rank.get(level, 0) >= required[difficulty]


def _required_success_level(difficulty: str) -> str:
    """把难度转换为花费幸运后显示的最低成功等级。"""

    return {"regular": "regular", "hard": "hard", "extreme": "extreme"}[difficulty]


def _luck_cost(run: CheckRun) -> int:
    """计算把当前失败骰点降到难度阈值所需的幸运点。"""

    details = run.details_json or {}
    if run.roll is None or details.get("success_level") == "fumble" or details.get("pushed"):
        return 0
    divisor = {"regular": 1, "hard": 2, "extreme": 5}[run.difficulty]
    return max(0, run.roll - max(1, run.target_value // divisor))


def _post_roll_options(run: CheckRun, actor: RuntimeActor) -> list[str]:
    """根据检定种类和角色资源生成可恢复的骰后选择。"""

    details = run.details_json or {}
    if not details.get("allow_luck") and not details.get("allow_push"):
        return []
    options = ["accept_failure"]
    cost = _luck_cost(run)
    if details.get("allow_luck") and 0 < cost <= int(actor.state_json.get("luck", 0)):
        options.append("spend_luck")
    if details.get("allow_push") and not details.get("pushed"):
        options.append("push")
    return options


def _check_roll_facts(state: dict[str, Any], run: CheckRun) -> list[str]:
    """把权威骰点转换为 Narrator 可复述的确定性事实。"""

    details = run.details_json or {}
    level_labels = {
        "critical": "大成功",
        "extreme": "极难成功",
        "hard": "困难成功",
        "regular": "成功",
        "failure": "失败",
        "fumble": "大失败",
    }
    level = str(details.get("success_level", "failure"))
    return [
        f"{_skill_fact_label(state, run.skill_id)}检定{level_labels[level]}，"
        f"骰点为 {run.roll}，技能值为 {run.target_value}。"
    ]


def _roll_formula(formula: int | str) -> int:
    """结算 ModulePack 中受限的整数或 NdM 骰式，不执行任意表达式。"""

    if isinstance(formula, int):
        return max(0, formula)
    match = re.fullmatch(r"(\d{1,2})d(\d{1,3})([+-]\d{1,3})?", formula.strip())
    if match is None:
        raise GmRuntimeError("模组包含不受支持的骰式")
    count, sides = int(match.group(1)), int(match.group(2))
    if count < 1 or sides < 2:
        raise GmRuntimeError("模组骰式范围无效")
    return max(
        0, sum(secrets.randbelow(sides) + 1 for _index in range(count)) + int(match.group(3) or 0)
    )


def _schedule_timeline_events(state: dict[str, Any], event_ids: list[str]) -> None:
    """按当前世界时间把声明的定时事件加入有序队列。"""

    runtime = state.get("_runtime", {})
    if not isinstance(runtime, dict):
        return
    definitions = {
        item.get("id"): item
        for item in runtime.get("timeline", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    queue = state.setdefault("scheduled_events", [])
    if not isinstance(queue, list):
        queue = []
        state["scheduled_events"] = queue
    existing = {item.get("id") for item in queue if isinstance(item, dict)}
    now = datetime.fromisoformat(str(state["world_time"]))
    for event_id in event_ids:
        definition = definitions.get(event_id)
        trigger = definition.get("trigger", {}) if isinstance(definition, dict) else {}
        minutes = trigger.get("after_minutes") if isinstance(trigger, dict) else None
        if event_id not in existing and isinstance(minutes, int) and minutes >= 0:
            queue.append({"id": event_id, "due_at": (now + timedelta(minutes=minutes)).isoformat()})
            existing.add(event_id)
    queue.sort(key=lambda item: str(item.get("due_at", "")) if isinstance(item, dict) else "")


def _next_scheduled_event(state: dict[str, Any], target_time: datetime) -> dict[str, Any] | None:
    """返回等待区间内最早的定时事件。"""

    current = datetime.fromisoformat(str(state["world_time"]))
    queue = state.get("scheduled_events", [])
    if not isinstance(queue, list):
        return None
    return next(
        (
            cast(dict[str, Any], item)
            for item in queue
            if isinstance(item, dict)
            and isinstance(item.get("due_at"), str)
            and current < datetime.fromisoformat(item["due_at"]) <= target_time
        ),
        None,
    )


def _apply_scheduled_event(
    state: dict[str, Any], actor: RuntimeActor, scheduled: dict[str, Any]
) -> list[str]:
    """移除并执行一个到期事件的 ModulePack 效果。"""

    queue = state.get("scheduled_events", [])
    if isinstance(queue, list):
        state["scheduled_events"] = [item for item in queue if item is not scheduled]
    definition = _runtime_definition(state, "timeline", str(scheduled.get("id", "")))
    effect = definition.get("effect", {}) if definition is not None else {}
    return _apply_effects(state, actor, effect if isinstance(effect, dict) else {})


def _apply_damage(actor_state: dict[str, Any], amount: int) -> None:
    """应用 HP、重伤、昏迷和死亡的最小 CoC7 伤害状态。"""

    hp = int(actor_state.get("hp", 0))
    max_hp = int(actor_state.get("max_hp", hp))
    actor_state["hp"] = max(0, hp - amount)
    if amount >= max(1, max_hp // 2):
        actor_state["major_wound"] = True
    if actor_state["hp"] == 0:
        actor_state["unconscious"] = True
        actor_state["alive"] = False


def _apply_effects(state: dict[str, Any], actor: RuntimeActor, effect: dict[str, Any]) -> list[str]:
    """解释 ModulePack 的通用效果集合并返回玩家可见事实。"""

    _add_clues(state, [str(clue) for clue in effect.get("clues", [])])
    if isinstance(effect.get("scene_id"), str):
        state["scene_id"] = effect["scene_id"]
    location_id = effect.get("location_id")
    if isinstance(location_id, str):
        actor.location_id = location_id
        state["location_id"] = location_id
        known = state.setdefault("known_locations", [])
        if isinstance(known, list) and location_id not in known:
            known.append(location_id)
    if isinstance(effect.get("ending_id"), str):
        state["ending_id"] = effect["ending_id"]
    flags = state.setdefault("flags", {})
    if isinstance(flags, dict) and isinstance(effect.get("flags"), dict):
        flags.update(effect["flags"])
    advance_minutes = effect.get("advance_minutes", 0)
    if isinstance(advance_minutes, int) and advance_minutes > 0:
        now = datetime.fromisoformat(str(state["world_time"]))
        state["world_time"] = (now + timedelta(minutes=advance_minutes)).isoformat()
    schedule = effect.get("schedule_events", [])
    if isinstance(schedule, list) and schedule:
        _schedule_timeline_events(state, [str(event_id) for event_id in schedule])
    npc_states = state.setdefault("npc_states", {})
    if isinstance(npc_states, dict) and isinstance(effect.get("npc_state"), dict):
        for npc_id, changes in effect["npc_state"].items():
            if isinstance(changes, dict):
                npc_states.setdefault(str(npc_id), {}).update(changes)
    if isinstance(effect.get("encounter"), dict):
        current_encounter = state.get("encounter")
        incoming = copy.deepcopy(effect["encounter"])
        if isinstance(current_encounter, dict) and current_encounter.get(
            "encounter_id"
        ) == incoming.get("encounter_id"):
            incoming["round"] = int(current_encounter.get("round", 1)) + 1
        state["encounter"] = incoming
    if isinstance(effect.get("encounter_update"), dict) and isinstance(
        state.get("encounter"), dict
    ):
        state["encounter"].update(effect["encounter_update"])
    damage_actor = effect.get("damage_actor")
    if isinstance(damage_actor, (int, str)):
        _apply_damage(actor.state_json, _roll_formula(damage_actor))
    damage_npc = effect.get("damage_npc")
    if isinstance(damage_npc, dict) and isinstance(npc_states, dict):
        npc_id = str(damage_npc.get("npc_id", ""))
        target = npc_states.setdefault(npc_id, {})
        amount = _roll_formula(damage_npc.get("amount", 0))
        divisor = int(damage_npc.get("armor_divisor", 1))
        amount = amount // max(1, divisor)
        _apply_damage(target, amount)
        encounter = state.get("encounter")
        if isinstance(encounter, dict) and encounter.get("opponent_id") == npc_id:
            encounter["opponent_hp"] = target.get("hp", 0)
            if target.get("alive") is False:
                encounter["status"] = "won"
        if target.get("alive") is False and isinstance(effect.get("on_npc_death"), dict):
            _apply_effects(state, actor, effect["on_npc_death"])
    san_loss = effect.get("san_loss")
    if isinstance(san_loss, (int, str)):
        loss = _roll_formula(san_loss)
        actor.state_json["san"] = max(0, int(actor.state_json.get("san", 0)) - loss)
        if loss >= 5:
            actor.state_json["temporary_insanity"] = True
            if isinstance(effect.get("temporary_insanity_ending"), str):
                state["ending_id"] = effect["temporary_insanity_ending"]
    return [str(fact) for fact in effect.get("facts", []) if isinstance(fact, str)]


def _encounter_read(value: object) -> EncounterRead | None:
    """把内部 Encounter 状态过滤为玩家投影。"""

    if not isinstance(value, dict):
        return None
    return EncounterRead.model_validate(value)


def _apply_check_outcome(
    state: dict[str, Any], actor: RuntimeActor, goal: str, success: bool
) -> list[str]:
    """按模组 checkpoint 应用检定结果，不从目标文字推断剧情。"""

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
            outcome = copy.deepcopy(outcome)
            outcome.setdefault("clues", checkpoint.get(clue_key, []))
            facts = _apply_effects(state, actor, outcome)
            public_clues = {
                str(clue.get("id")): str(clue.get("text"))
                for clue in runtime.get("clues", [])
                if isinstance(clue, dict)
                and clue.get("visibility") == "public"
                and isinstance(clue.get("id"), str)
                and isinstance(clue.get("text"), str)
            }
            return facts or [
                public_clues[clue_id]
                for clue_id in outcome.get("clues", [])
                if clue_id in public_clues
            ]
    return []


async def submit_free_text(
    db: AsyncSession,
    *,
    room_id: str,
    payload: TurnInputBody,
) -> GmTurnRead:
    """解释自然语言并按最新 revision 顺序执行全部合法步骤。"""

    previous = await db.scalar(
        select(TurnRun).where(TurnRun.client_request_id == payload.client_request_id)
    )
    if previous is not None and previous.room_id != room_id:
        raise GmRuntimeError("重复请求 ID 已属于其他房间")
    if previous is not None and previous.result_json is not None and previous.status != "paused":
        return GmTurnRead.model_validate(previous.result_json)
    turn = previous or TurnRun(
        room_id=room_id,
        client_request_id=payload.client_request_id,
        status="interpreting",
        expected_revision=payload.expected_revision,
        actor_id=payload.actor_id,
        input_text=payload.input,
    )
    if previous is None:
        db.add(turn)
        await db.commit()
    else:
        # 失败回合只恢复模型调用，不重放已经提交的 Kernel 命令。
        turn.status = "interpreting"
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
        turn.status = "validating"
        await db.commit()
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
        turn.status = "resolving"
        await db.commit()
        revision = payload.expected_revision
        step_results: list[CommandResult] = []
        for index, step in enumerate(intent.steps):
            step_snapshot = (
                snapshot
                if index == 0
                else await build_context_snapshot(db, room_id=room_id, actor_id=payload.actor_id)
            )
            validate_adjudication(step_snapshot, propose_adjudication(step_snapshot, step))
            step_request_id = (
                payload.client_request_id
                if index == 0
                else f"{payload.client_request_id}:{index + 1}"
            )
            command = intent_step_to_command(
                step,
                client_request_id=step_request_id,
                expected_revision=revision,
                actor_id=payload.actor_id,
            )
            step_result = await submit_command(db, room_id=room_id, envelope=command)
            step_results.append(step_result)
            revision = step_result.revision
            if step_result.pending_decisions:
                break
        if not step_results:
            raise GmRuntimeError("模型没有提出可执行步骤")
        last_result = step_results[-1]
        command_result = last_result.model_copy(
            update={
                "client_request_id": payload.client_request_id,
                "events": [event for result in step_results for event in result.events],
                "narration_facts": [
                    fact for result in step_results for fact in result.narration_facts
                ],
            }
        )
        narration: str | None = None
        try:
            turn.status = "narrating"
            await db.commit()
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
        turn.status = "publishing"
        await db.commit()
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
        turn.status = "paused"
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

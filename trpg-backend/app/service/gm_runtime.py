"""Phase 0 GM Kernel 的会话安装、Wait 命令和幂等回执服务。"""

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.dto.gm import (
    CheckRead,
    ClarificationRead,
    CommandEnvelope,
    CommandResult,
    DomainEventEnvelope,
    GmTurnRead,
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
from app.service.gm_ai import (
    AgentsSdkInterpreter,
    AgentsSdkNarrator,
    GmModelUnavailable,
    IntentInterpreter,
    Narrator,
    build_context_snapshot,
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
    "kimball_house": {"desk", "empty_shelf", "search_study", "read_diary", "surveillance"},
    "cemetery": {
        "graveyard_gate",
        "headstone",
        "gravekeeper",
        "track_grave",
        "night_watch",
        "open_crypt",
        "call_douglas",
        "attack_douglas",
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
        module_version = ModuleVersion(
            module_id=scenario.id,
            version=pack.version,
            world_ref="coc7",
            content_schema_version=1,
            # 目录用于房间展示，runtime 是同一版本的结构化规则切片。
            content_json={"catalog": pack.catalog, "runtime": pack.runtime},
        )
        db.add(module_version)
        await db.flush()
    now = datetime.now(UTC)
    runtime = pack.runtime
    initial = dict(runtime.get("initial_state", {}))
    session = GameSession(
        room_id=room_id,
        module_id=scenario.id,
        module_version=pack.version,
        state_schema_version=1,
        state_json={
            "world_time": now.isoformat(),
            "location_id": "arnoldsburg",
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
        display_name=display_name,
        location_id="arnoldsburg",
        state_json={
            "alive": True,
            "hp": int(initial.get("hp", 10)),
            "san": int(initial.get("san", 50)),
            "items": list(initial.get("items", [])),
        },
        created_at=now,
    )
    db.add_all([session, actor])
    await db.commit()
    return _session_read(session, actor)


async def submit_command(
    db: AsyncSession,
    *,
    room_id: str,
    envelope: CommandEnvelope,
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
    state = dict(session.state_json)
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
        narration_facts = [
            f"时间从 {current_time.isoformat()} 推进到 {effective_time.isoformat()}。"
        ]
        if interrupted:
            state["next_interrupt_at"] = None
            if state.get("flags", {}).get("night_watch"):
                state["scene_id"] = "confrontation"
    elif command.kind == "move_actor":
        exits = _LOCATIONS.get(actor.location_id, set())
        if command.target_id not in exits:
            raise GmRuntimeError("目标地点不是当前地点的合法出口")
        previous_location = actor.location_id
        actor.location_id = command.target_id
        state["scene_id"] = _scene_for_location(state, command.target_id)
        event_type = "actor_moved"
        event_payload = {"from": previous_location, "to": command.target_id}
        narration_facts = [f"你从 {previous_location} 来到了 {command.target_id}。"]
    elif command.kind == "inspect_target" or command.kind == "talk_to_npc":
        if command.target_id not in _TARGETS.get(actor.location_id, set()):
            raise GmRuntimeError("目标不在当前地点的可见对象中")
        event_type = "target_inspected" if command.kind == "inspect_target" else "npc_contacted"
        event_payload = {"target_id": command.target_id, "topic": getattr(command, "topic", "")}
        narration_facts = [f"你完成了对 {command.target_id} 的行动。"]
        _apply_module_target(state, actor, command.target_id, getattr(command, "topic", ""))
    elif command.kind == "start_check":
        skill = _SKILLS.get(command.skill_id)
        if skill is None:
            raise GmRuntimeError("技能未在当前规则切片中实现")
        if await db.get(CheckRun, command.check_id) is not None:
            raise GmRuntimeError("check_id 已存在")
        target_value = int(actor.state_json.get("skills", {}).get(command.skill_id, skill["base"]))
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
        narration_facts = [f"你准备对 {command.goal} 使用 {command.skill_id} 检定。"]
        pending = [_pending_read(decision)]
        check = _check_read(run)
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
        check = _check_read(run)
        event_type = "check_resolved"
        event_payload = {
            "check_id": run.id,
            "roll": roll,
            "target_value": target_value,
            "success": run.success,
        }
        narration_facts = [
            f"{run.skill_id} 检定结果为 {'成功' if run.success else '失败'}，骰点 {roll}。"
        ]
        _apply_check_outcome(state, actor, run.goal, bool(run.success))
    elif command.kind == "choose_option":
        _apply_module_target(state, actor, command.option_id, command.option_id)
        if state.get("ending_id") is None:
            raise GmRuntimeError("该剧情选择当前不可用")
        event_type = "ending_committed"
        event_payload = {"ending_id": state["ending_id"]}
        narration_facts = [f"结局已确定：{state['ending_id']}。"]
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
    db.add(
        CommandReceipt(
            room_id=room_id,
            client_request_id=envelope.client_request_id,
            revision=new_revision,
            result_json=result_json,
            created_at=datetime.now(UTC),
        )
    )
    db.add(
        OutboxMessage(
            room_id=room_id,
            event_id=event_id,
            payload=result_json,
            created_at=datetime.now(UTC),
        )
    )
    await db.commit()
    return result


def _pending_read(record: PendingDecisionRecord) -> PendingDecision:
    """把数据库待决策转换为不含内部字段的玩家 DTO。"""

    return PendingDecision(
        decision_id=record.id,
        kind="roll_check",
        check_id=record.check_id,
        options=list(record.options),
    )


def _check_read(run: CheckRun) -> CheckRead:
    """把检定内部记录转换为玩家可见结果。"""

    return CheckRead(
        check_id=run.id,
        skill_id=run.skill_id,
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
    return PlayerProjection(
        session_id=session.room_id,
        actor_id=actor.id,
        revision=session.state_version,
        world_time=datetime.fromisoformat(session.state_json["world_time"]),
        location_id=actor.location_id,
        visible_facts=list(session.state_json.get("visible_facts", [])),
        scene_id=scene_id,
        scene_label=scene.get("label"),
        clues=list(session.state_json.get("clues", [])),
        hp=actor.state_json.get("hp"),
        san=actor.state_json.get("san"),
        ending_id=session.state_json.get("ending_id"),
    )


def _runtime_from_session(session: GameSession) -> dict[str, Any]:
    """读取会话冻结的结构化运行包，避免运行中再次读取原始模组文件。"""

    runtime = session.state_json.get("_runtime", {})
    return cast(dict[str, Any], runtime) if isinstance(runtime, dict) else {}


def _scene_for_location(state: dict[str, object], location_id: str) -> str:
    """把移动后的地点映射为公开场景，保持旧移动命令兼容。"""

    current = str(state.get("scene_id", "briefing"))
    if location_id == "library":
        return "library"
    if location_id == "newspaper":
        return "newspaper"
    if location_id == "kimball_house":
        return "kimball_house"
    if location_id == "cemetery" and current not in {"confrontation", "crypt", "douglas"}:
        return "cemetery"
    if location_id == "crypt":
        return "crypt"
    return current


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
        state["ending_id"] = "douglas_killed"
    elif target_id == "flee" or (target_id == "leave" and flags.get("douglas_alive") is False):
        state["ending_id"] = "flee"


def _apply_check_outcome(
    state: dict[str, Any], actor: RuntimeActor, goal: str, success: bool
) -> None:
    """把检定结果映射为公开线索或失败前进，不让模型直接写状态。"""

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
        interpreter, narrator = _agents()
        intent = validate_intent(snapshot, await interpreter.interpret(snapshot, payload.input))
        if intent.kind == "clarification":
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
            refreshed = await build_context_snapshot(db, room_id=room_id, actor_id=payload.actor_id)
            draft = await narrator.narrate(
                refreshed,
                [event.event_id for event in command_result.events],
                command_result.narration_facts,
            )
            narration = guard_narration(
                draft,
                committed_event_ids=[event.event_id for event in command_result.events],
                visible_facts=command_result.narration_facts,
            ).text
        except (GmModelUnavailable, ValueError):
            # Kernel 已提交时不能回滚或伪造主持；前端仍可展示 CommandResult 的确定性事实。
            narration = None
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
    clarification_record = await db.scalar(
        select(TurnRun)
        .where(
            TurnRun.room_id == room_id,
            TurnRun.actor_id == actor_id,
            TurnRun.status == "awaiting_clarification",
        )
        .order_by(TurnRun.created_at.desc())
        .limit(1)
    )
    clarification: ClarificationRead | None = None
    if clarification_record is not None and clarification_record.result_json is not None:
        turn_result = GmTurnRead.model_validate(clarification_record.result_json)
        if turn_result.clarification_question:
            clarification = ClarificationRead(
                client_request_id=turn_result.client_request_id,
                question=turn_result.clarification_question,
                options=turn_result.clarification_options,
            )
    return _projection(session, actor).model_copy(
        update={
            "pending_decisions": [_pending_read(decision) for decision in decisions],
            "checks": [_check_read(run) for run in checks],
            "pending_clarification": clarification,
        }
    )

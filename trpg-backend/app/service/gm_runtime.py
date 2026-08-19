"""Phase 0 GM Kernel 的会话安装、Wait 命令和幂等回执服务。"""

import secrets
import uuid
from datetime import UTC, datetime
from typing import Literal, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dto.gm import (
    CheckRead,
    CommandEnvelope,
    CommandResult,
    DomainEventEnvelope,
    PendingDecision,
    PlayerProjection,
    SessionRead,
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
)
from app.service.module_runtime import ModulePackError, load_preset


class GmRuntimeError(ValueError):
    """GM 会话或命令无法通过 Kernel 校验。"""


# Phase 1A 只冻结《追书人》第一条单人调查需要的对象；后续完整 ModulePack
# 会把同样的结构移入模组运行包，Kernel 仍只消费结构化定义。
_LOCATIONS = {
    "arnoldsburg": {"library", "kimball_house", "cemetery"},
    "library": {"arnoldsburg"},
    "kimball_house": {"arnoldsburg"},
    "cemetery": {"arnoldsburg"},
}
_TARGETS = {
    "arnoldsburg": {"town_sign", "library", "kimball_house", "cemetery"},
    "library": {"old_newspapers", "bookshelf", "librarian"},
    "kimball_house": {"desk", "empty_shelf"},
    "cemetery": {"graveyard_gate", "headstone"},
}
_SKILLS = {
    "spot-hidden": {"base": 25, "purpose": "发现不明显的物体、痕迹或异常"},
    "library-use": {"base": 20, "purpose": "检索和理解图书馆、报纸与档案资料"},
    "listen": {"base": 20, "purpose": "发现听觉上不明显的声音或动静"},
    "persuade": {"base": 10, "purpose": "以合理承诺或论证说服他人"},
    "charm": {"base": 15, "purpose": "以友善态度建立短暂信任"},
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
            content_json=pack.catalog,
        )
        db.add(module_version)
        await db.flush()
    now = datetime.now(UTC)
    session = GameSession(
        room_id=room_id,
        module_id=scenario.id,
        module_version=pack.version,
        state_schema_version=1,
        state_json={
            "world_time": now.isoformat(),
            "location_id": "arnoldsburg",
            "visible_facts": [],
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
        state_json={"alive": True},
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
        if interrupted:
            state["next_interrupt_at"] = None
    elif command.kind == "move_actor":
        exits = _LOCATIONS.get(actor.location_id, set())
        if command.target_id not in exits:
            raise GmRuntimeError("目标地点不是当前地点的合法出口")
        previous_location = actor.location_id
        actor.location_id = command.target_id
        event_type = "actor_moved"
        event_payload = {"from": previous_location, "to": command.target_id}
    elif command.kind == "inspect_target" or command.kind == "talk_to_npc":
        if command.target_id not in _TARGETS.get(actor.location_id, set()):
            raise GmRuntimeError("目标不在当前地点的可见对象中")
        event_type = "target_inspected" if command.kind == "inspect_target" else "npc_contacted"
        event_payload = {"target_id": command.target_id, "topic": getattr(command, "topic", "")}
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
    projection = _projection(session, actor)
    result = CommandResult(
        client_request_id=envelope.client_request_id,
        revision=new_revision,
        events=[event],
        projection=projection,
        pending_decisions=pending,
        check=check,
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

    return SessionRead(
        session_id=session.room_id,
        module_id=session.module_id,
        module_version=session.module_version,
        projection=_projection(session, actor),
    )


def _projection(session: GameSession, actor: RuntimeActor) -> PlayerProjection:
    """只选择玩家可见字段，避免把 keeper 状态扩散到 API。"""

    return PlayerProjection(
        session_id=session.room_id,
        actor_id=actor.id,
        revision=session.state_version,
        world_time=datetime.fromisoformat(session.state_json["world_time"]),
        location_id=actor.location_id,
        visible_facts=list(session.state_json.get("visible_facts", [])),
    )

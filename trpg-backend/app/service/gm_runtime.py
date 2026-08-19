"""Phase 0 GM Kernel 的会话安装、Wait 命令和幂等回执服务。"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dto.gm import (
    CommandEnvelope,
    CommandResult,
    DomainEventEnvelope,
    PlayerProjection,
    SessionRead,
)
from app.models.content import Scenario
from app.models.gm import (
    CommandReceipt,
    GameEvent,
    GameSession,
    ModuleVersion,
    OutboxMessage,
    RuntimeActor,
)
from app.service.module_runtime import ModulePackError, load_preset


class GmRuntimeError(ValueError):
    """GM 会话或命令无法通过 Kernel 校验。"""


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
    if envelope.command.kind != "wait_until":
        raise GmRuntimeError("Phase 0 仅实现 wait_until")
    state = dict(session.state_json)
    target_time = envelope.command.target_time
    current_time = datetime.fromisoformat(state["world_time"])
    if target_time <= current_time:
        raise GmRuntimeError("等待时间必须晚于当前世界时间")
    new_revision = session.state_version + 1
    event_id = str(uuid.uuid4())
    event = DomainEventEnvelope(
        event_id=event_id,
        event_type="time_advanced",
        actor_id=envelope.actor_id,
        payload={"from": current_time.isoformat(), "to": target_time.isoformat()},
    )
    state["world_time"] = target_time.isoformat()
    session.state_json = state
    session.state_version = new_revision
    session.updated_at = datetime.now(UTC)
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

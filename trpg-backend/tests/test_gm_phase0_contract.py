"""Phase 0 DTO 与预设 ModulePack 的最小契约测试。"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.dto.gm import CommandEnvelope
from app.models.room import Room
from app.service.gm_runtime import create_session, submit_command
from app.service.module_runtime import ModulePackError, load_preset


def test_command_rejects_unknown_fields() -> None:
    """模型多输出字段时必须在边界拒绝，而不是静默丢弃。"""

    with pytest.raises(ValidationError):
        CommandEnvelope.model_validate(
            {
                "client_request_id": "request-1",
                "expected_revision": 0,
                "actor_id": "actor-1",
                "command": {
                    "kind": "wait_until",
                    "target_time": datetime.now(UTC).isoformat(),
                    "write_state": {"san": 0},
                },
            }
        )


def test_paper_chase_preset_has_static_catalog() -> None:
    """《追书人》目录应可在无模型情况下加载。"""

    pack = load_preset("paper_chase")
    assert pack.title == "追书人"
    assert pack.catalog["story_pages"]


def test_module_pack_rejects_invalid_catalog(tmp_path: Path) -> None:
    """manifest 与 catalog 不一致时不能安装半成品运行包。"""

    (tmp_path / "manifest.json").write_text(
        '{"module_id":"bad","title":"A","content_version":"v1","catalog_file":"catalog.json"}',
        encoding="utf-8",
    )
    (tmp_path / "catalog.json").write_text('{"title":"B","story_pages":[]}', encoding="utf-8")
    with pytest.raises(ModulePackError, match="标题不一致"):
        from app.service.module_runtime import load_module_pack

        load_module_pack(tmp_path)


async def test_wait_command_is_idempotent(db_session) -> None:
    """同一 client_request_id 重试只返回原回执，不重复推进 revision。"""

    room_id = "00000000-0000-0000-0000-000000000099"
    db_session.add(
        Room(
            id=room_id,
            room_code="GMTEST",
            room_name="Phase 0",
            max_players=1,
        )
    )
    await db_session.commit()
    session = await create_session(
        db_session,
        room_id=room_id,
        module_id="paper-chase",
        actor_id="actor-1",
        display_name="调查员",
    )
    target = session.projection.world_time + timedelta(hours=2)
    command = CommandEnvelope(
        client_request_id="wait-1",
        expected_revision=0,
        actor_id="actor-1",
        command={"kind": "wait_until", "target_time": target},
    )
    first = await submit_command(db_session, room_id=room_id, envelope=command)
    second = await submit_command(db_session, room_id=room_id, envelope=command)
    assert first.revision == second.revision == 1
    assert first.events[0].event_id == second.events[0].event_id

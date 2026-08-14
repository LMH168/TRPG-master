"""在 CI PostgreSQL 上验证可靠回合迁移、提交回执与叙事 Outbox。

脚本只使用临时事务，结束时统一回滚，不向数据库留下测试数据；它不会读取模型
凭据，也不会发起除目标 PostgreSQL 连接以外的网络请求。
"""

import asyncio
import json
import os
from datetime import UTC, datetime
from uuid import uuid4

import asyncpg


async def main() -> None:
    """迁移完成后写入一组关联记录，并核对 PostgreSQL 约束实际生效。"""

    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)
    connection = await asyncpg.connect(url)
    transaction = connection.transaction()
    await transaction.start()
    try:
        now = datetime.now(UTC)
        room_id = uuid4()
        player_id = uuid4()
        turn_id = uuid4()
        outbox_id = uuid4()
        reconnect_token = uuid4()
        client_action_id = "postgres-turn-check"
        request = {
            "schema_version": 1,
            "room_id": str(room_id),
            "player_id": str(player_id),
            "actor_id": "postgres-actor",
            "client_action_id": client_action_id,
            "utterance": "检查 PostgreSQL 事务边界",
        }

        # 先创建最小房间成员，再写入完整的 Turn → receipt → Outbox 外键链。
        await connection.execute(
            """
            INSERT INTO rooms (
                id, room_code, room_name, max_players, phase, discovered_scene_ids,
                created_at, updated_at
            ) VALUES ($1, $2, $3, 4, 'InGame', '[]'::json, $4, $4)
            """,
            room_id,
            uuid4().hex[:6].upper(),
            "PostgreSQL Turn Check",
            now,
        )
        await connection.execute(
            """
            INSERT INTO players (
                id, room_id, is_ai, nickname, is_host, ready, has_character,
                reconnect_token, connected, joined_at
            ) VALUES ($1, $2, false, 'CI Player', true, true, true, $3, true, $4)
            """,
            player_id,
            room_id,
            reconnect_token,
            now,
        )
        await connection.execute(
            """
            INSERT INTO turn_records (
                turn_id, room_id, client_action_id, input_fingerprint, player_id,
                actor_id, request_schema_version, request_json, status, phase_version,
                resume_point, waiting_reason, commit_state, recovery_action,
                created_at, updated_at
            ) VALUES (
                $1, $2, $3, $4, $5, 'postgres-actor', 1, $6::json,
                'awaiting_narration', 4, 'narrating', 'none', 'committed', 'wait',
                $7, $7
            )
            """,
            turn_id,
            room_id,
            client_action_id,
            "0" * 64,
            player_id,
            json.dumps(request, ensure_ascii=False),
            now,
        )
        await connection.execute(
            """
            INSERT INTO room_turn_reservations (room_id, turn_id, created_at, updated_at)
            VALUES ($1, $2, $3, $3)
            """,
            room_id,
            turn_id,
            now,
        )
        # 无 DomainEvent 的已完成命令也必须可以用 receipt 判定提交成功。
        await connection.execute(
            """
            INSERT INTO turn_commit_receipts (
                room_id, engine_request_id, turn_id, action_request_id,
                committed_state_version, first_event_sequence, last_event_sequence, created_at
            ) VALUES ($1, 'engine-request-1', $2, 'action-step-1', 1, NULL, NULL, $3)
            """,
            room_id,
            turn_id,
            now,
        )
        await connection.execute(
            """
            INSERT INTO narration_outbox (
                outbox_id, turn_id, room_id, player_id, message_id, message_type,
                visibility, payload_schema_version, payload_json, status, attempt_count,
                next_attempt_at, created_at, updated_at
            ) VALUES (
                $1, $2, $3, $4, 'postgres-message-1', 'narration.push',
                'player_scoped', 1, $5::json, 'pending', 0, $6, $6, $6
            )
            """,
            outbox_id,
            turn_id,
            room_id,
            player_id,
            json.dumps({"text": "PostgreSQL Outbox"}),
            now,
        )

        linked = await connection.fetchrow(
            """
            SELECT r.turn_id, r.first_event_sequence, o.visibility
            FROM turn_commit_receipts AS r
            JOIN narration_outbox AS o ON o.turn_id = r.turn_id
            WHERE r.room_id = $1 AND r.engine_request_id = 'engine-request-1'
            """,
            room_id,
        )
        assert linked is not None and linked["turn_id"] == turn_id
        assert linked["first_event_sequence"] is None
        assert linked["visibility"] == "player_scoped"

        indexes = {
            row["indexname"]
            for row in await connection.fetch(
                """
                SELECT indexname FROM pg_indexes
                WHERE indexname IN (
                    'uq_turn_records_room_client_action',
                    'uq_room_turn_reservations_turn',
                    'uq_narration_outbox_turn_type'
                )
                """
            )
        }
        assert indexes == {
            "uq_turn_records_room_client_action",
            "uq_room_turn_reservations_turn",
            "uq_narration_outbox_turn_type",
        }
        print("PostgreSQL reliable turn migration/receipt/outbox check passed")
    finally:
        # CI 数据库也不应积累探针数据，成功或失败都回滚整组写入。
        await transaction.rollback()
        await connection.close()


if __name__ == "__main__":
    asyncio.run(main())

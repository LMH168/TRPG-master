"""为基础账号、房间、角色、聊天和生图测试提供隔离数据库。"""

import tempfile
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.controller import ws as ws_controller
from app.core.db import Base, get_db
from app.core.seed import ensure_seed_content
from app.main import app
from app.service.character_background import CharacterBackgroundService

_TEST_DB_PATH = Path(tempfile.mkdtemp(prefix="trpg-test-")) / "test.db"
test_engine = create_async_engine(f"sqlite+aiosqlite:///{_TEST_DB_PATH}", poolclass=NullPool)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)


async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
    """让 HTTP 测试统一使用临时数据库。"""

    async with TestSessionLocal() as session:
        yield session


app.dependency_overrides[get_db] = override_get_db
app.state.character_background_service = CharacterBackgroundService()
app.state.test_session_factory = TestSessionLocal
ws_controller.async_session_factory = TestSessionLocal  # type: ignore[assignment]


@pytest.fixture(autouse=True)
async def _prepare_database() -> AsyncGenerator[None, None]:
    """每个测试前重建基础表并填充目录种子。"""

    async with test_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with TestSessionLocal() as session:
        await ensure_seed_content(session)
    yield
    async with test_engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """提供可直接验证 ORM 状态的测试会话。"""

    async with TestSessionLocal() as session:
        yield session


@pytest.fixture
def sql_counter() -> Generator[list[str], None, None]:
    """记录测试期间执行的 SQL，用于检测 N+1 查询。"""

    from sqlalchemy import event

    executed: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        executed.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", record)
    try:
        yield executed
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", record)


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """提供无需启动真实端口的 FastAPI 客户端。"""

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client

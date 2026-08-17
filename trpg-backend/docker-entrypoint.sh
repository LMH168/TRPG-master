#!/bin/sh
# CI Preview 容器启动入口（issue #200）。数据库迁移仍在进程启动前完成；
# 内置模组由 FastAPI startup 统一幂等发布，避免容器与本地 uvicorn 使用两套流程。
set -e

uv run alembic upgrade head
exec uv run uvicorn app.main:app --host 0.0.0.0 --port 8000

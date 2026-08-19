"""健康检查接口的响应形状，顺便验证统一响应信封在最简单的接口上长什么样。"""

from httpx import AsyncClient


async def test_health_endpoint(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["status"] in {"ok", "degraded"}
    assert body["data"]["storage"] == "ok"
    assert body["data"]["model"] in {"configured", "unconfigured"}
    assert body["data"]["provider"] == "deepseek"

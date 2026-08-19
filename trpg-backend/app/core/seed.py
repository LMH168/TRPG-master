"""开发/测试环境的最小内容种子数据。

内容库（`games`/`game_systems`/`scenarios`）本期没有真实的模组管理后台，
`GET /modules` 等目录接口至少需要一条可选模组，"注册 → 建房 → 选模组 →
开局"这条主线才能继续跑通（issue"不回归"验收标准）——原来内存 stub 里硬编码
的 `_BUILTIN_MODULES` 现在改用这份种子数据落进真实数据库。COC7 系统还额外
带上 `app/core/coc7_content.py` 的规则数据（属性/技能/职业目录），供
`GET /systems/{systemId}/ruleset` 返回。

清理旧 AI 主持运行时后，``Scenario`` 只保存目录和展示信息；Seed 不再内嵌、
解析或发布任何结构化执行内容。

用固定 UUID + 幂等插入（先查是否已存在）：应用启动时、测试 fixture 里都可以
放心重复调用，不会插入重复数据。
"""

import json
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.coc7_content import build_coc7_ruleset
from app.models.content import Game, GameSystem, Scenario, World

BUILTIN_GAME_ID = "00000000-0000-0000-0000-000000000001"
BUILTIN_SYSTEM_ID = "00000000-0000-0000-0000-000000000002"
BUILTIN_SCENARIO_ID = "00000000-0000-0000-0000-000000000003"
BUILTIN_WORLD_ID = "00000000-0000-0000-0000-000000000004"
BUILTIN_MODULE_ID = "paper-chase"
BUILTIN_WORLD_REF = "coc7-1920s"

_PAPER_CHASE_CATALOG = (
    Path(__file__).resolve().parents[2] / "modules" / "presets" / "追书人" / "catalog.json"
)


def _load_paper_chase_catalog() -> dict:
    """读取预设目录中的公开展示信息，避免把模组内容硬编码在种子代码里。"""
    try:
        catalog = json.loads(_PAPER_CHASE_CATALOG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取《追书人》目录数据：{_PAPER_CHASE_CATALOG}") from exc

    pages = catalog.get("story_pages")
    if not isinstance(pages, list) or not all(
        isinstance(page, dict)
        and isinstance(page.get("title"), str)
        and isinstance(page.get("content"), str)
        for page in pages
    ):
        raise RuntimeError("《追书人》目录数据的 story_pages 格式无效")
    return catalog


async def ensure_seed_content(db: AsyncSession) -> None:
    """插入基础规则系统和目录展示数据，不发布任何 AI 主持运行时内容。"""
    catalog = _load_paper_chase_catalog()
    coc7_ruleset = build_coc7_ruleset().model_dump(mode="json")

    game = await db.get(Game, BUILTIN_GAME_ID)
    if game is None:
        db.add(
            Game(
                id=BUILTIN_GAME_ID,
                name="克苏鲁的呼唤",
                description=(
                    "在熟悉的现实世界表层之下，调查员通过走访、检索与推理接触不可名状的宇宙恐怖；"
                    "重视调查、角色扮演与理智风险，正面战斗通常不是首选。"
                ),
                tags=["1920年代", "调查悬疑", "宇宙恐怖"],
            )
        )
    else:
        game.name = "克苏鲁的呼唤"
        game.description = (
            "在熟悉的现实世界表层之下，调查员通过走访、检索与推理接触不可名状的宇宙恐怖；"
            "重视调查、角色扮演与理智风险，正面战斗通常不是首选。"
        )
        game.tags = ["1920年代", "调查悬疑", "宇宙恐怖"]

    world = await db.get(World, BUILTIN_WORLD_ID)
    if world is None:
        db.add(
            World(
                id=BUILTIN_WORLD_ID,
                game_id=BUILTIN_GAME_ID,
                name="禁酒令时期的阿诺兹堡",
                description=(
                    "1920 年代美国密歇根州的小城。旧书、失踪案与不可名状的恐怖交织，"
                    "调查员需要依靠走访、观察和谨慎判断逐步还原真相。"
                ),
            )
        )

    system = await db.get(GameSystem, BUILTIN_SYSTEM_ID)
    if system is None:
        db.add(
            GameSystem(
                id=BUILTIN_SYSTEM_ID,
                game_id=BUILTIN_GAME_ID,
                world_ref=BUILTIN_WORLD_REF,
                name="COC7",
                version="7th",
                ruleset=coc7_ruleset,
            )
        )
    else:
        # 内置规则与稳定 world_ref 随代码发版，数据库副本每次启动都跟代码对齐。
        system.world_ref = BUILTIN_WORLD_REF
        system.ruleset = coc7_ruleset

    scenario = await db.get(Scenario, BUILTIN_SCENARIO_ID)
    if scenario is None:
        scenario = Scenario(
            id=BUILTIN_SCENARIO_ID,
            module_id=BUILTIN_MODULE_ID,
            world_id=BUILTIN_WORLD_ID,
            game_system_id=BUILTIN_SYSTEM_ID,
            title=catalog["title"],
            version=catalog["version"],
            authors=catalog["authors"],
            players_min=catalog["players_min"],
            players_max=catalog["players_max"],
            difficulty=catalog["difficulty"],
            estimated_duration=catalog["estimated_duration"],
            synopsis=catalog["synopsis"],
            status="ready",
            name_en=catalog["name_en"],
            story_label=catalog["story_label"],
            subtitle=catalog["subtitle"],
            story_pages=catalog["story_pages"],
        )
        db.add(scenario)
    else:
        scenario.module_id = BUILTIN_MODULE_ID
        scenario.world_id = BUILTIN_WORLD_ID
        scenario.status = "ready"
        # 目录展示内容随代码幂等更新，不涉及任何旧主持运行状态。
        scenario.title = catalog["title"]
        scenario.version = catalog["version"]
        scenario.authors = catalog["authors"]
        scenario.players_min = catalog["players_min"]
        scenario.players_max = catalog["players_max"]
        scenario.difficulty = catalog["difficulty"]
        scenario.estimated_duration = catalog["estimated_duration"]
        scenario.synopsis = catalog["synopsis"]
        scenario.name_en = catalog["name_en"]
        scenario.story_label = catalog["story_label"]
        scenario.subtitle = catalog["subtitle"]
        scenario.story_pages = catalog["story_pages"]

    await db.commit()

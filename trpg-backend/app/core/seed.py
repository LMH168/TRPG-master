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

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.coc7_content import build_coc7_ruleset
from app.models.content import Game, GameSystem, Scenario, World

BUILTIN_GAME_ID = "00000000-0000-0000-0000-000000000001"
BUILTIN_SYSTEM_ID = "00000000-0000-0000-0000-000000000002"
BUILTIN_SCENARIO_ID = "00000000-0000-0000-0000-000000000003"
BUILTIN_WORLD_ID = "00000000-0000-0000-0000-000000000004"
BUILTIN_MODULE_ID = "paper-chase"
BUILTIN_MODULE_VERSION = "catalog-1"
BUILTIN_WORLD_REF = "coc7-1920s"

# 这是房间开局前展示给玩家的静态介绍，不包含守秘人秘密或规则执行数据。
_PAPER_CHASE_STORY_PAGES = [
    {
        "title": "调查委托",
        "content": (
            "故事发生在禁酒令时期的美国密歇根州阿诺兹堡。托马斯·金博尔请你调查叔叔"
            "道格拉斯一年前的失踪，以及最近从叔叔旧居被盗的五本珍藏旧书。你需要寻找"
            "窃贼、尽可能追回书籍，并确认道格拉斯是否尚在人世。"
        ),
    },
    {
        "title": "调查员准备",
        "content": (
            "本模组由一名调查员进行，调查将以走访、资料检索和现场观察为主。擅长交涉、"
            "侦查或图书馆使用的调查员可能更容易推进调查，但这些能力并非必需。"
        ),
    },
]


async def ensure_seed_content(db: AsyncSession) -> None:
    """插入基础规则系统和目录展示数据，不发布任何 AI 主持运行时内容。"""
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
            title="追书人",
            version=BUILTIN_MODULE_VERSION,
            authors=["Chaosium"],
            players_min=1,
            players_max=1,
            difficulty=1,
            estimated_duration="1-2 小时",
            synopsis="一项围绕失踪者、旧书与墓地展开的调查。",
            status="ready",
            name_en="Paper Chase",
            story_label="调查记录",
            subtitle="旧书与失踪之谜",
            story_pages=_PAPER_CHASE_STORY_PAGES,
        )
        db.add(scenario)
    else:
        scenario.module_id = BUILTIN_MODULE_ID
        scenario.world_id = BUILTIN_WORLD_ID
        scenario.status = "ready"
        # 目录展示内容随代码幂等更新，不涉及任何旧主持运行状态。
        scenario.story_pages = _PAPER_CHASE_STORY_PAGES

    await db.commit()

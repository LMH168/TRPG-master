# ModulePackage 框架与《追书人》成品对照

本文用于评审模组解析 Agent 的最终产物框架。评审时同时查看：

- 空框架：[`module-package.template.json`](module-package.template.json)
- 成品样例：[`paper-chase/module-package.json`](paper-chase/module-package.json)
- 产物契约：[`parser-runtime-contract.md`](parser-runtime-contract.md)

空框架说明 Agent 必须填什么；《追书人》展示填完并通过自动门禁后的结果。空框架不是运行时数据，只有 `package_status=ready` 且 `validation.status=passed` 的成品包才能加载。

## 1. 顶层对照

| 框架字段 | Agent 要填入什么 | 《追书人》成品 |
|---|---|---|
| `package_schema_version` | 产物协议版本 | `1.0.0` |
| `package_id` | 模组、语言、规则版本和 revision 组成的唯一包 ID | `module-package.paper-chase-zh-coc7.v1` |
| `package_status` | 自动门禁通过后设为 `ready`，失败时不生成包 | `ready` |
| `source_manifest` | 文件、页数、语言、格式和提取方式 | `追书人.pdf`、6 页、`zh-CN` |
| `module` | 产品目录信息、规则绑定、人数、简介和入口场景 | CoC7、单人、入口为委托场景 |
| `keeper_brief` | 核心真相、体验目标、主持约束和防剧透 Fact | 道格拉斯主动成为食尸鬼，默认不敌对 |
| `runtime_defaults` | 未逐项声明时的确定性运行规则 | 线索获得前锁定、Trigger 默认一次 |
| `content` | 完整的叙事、规则和编排对象 | 5 Fact、12 Scene、12 Entity 等 |
| `initial_state` | 创建第一份 GameState 所需内容 | 委托场景、两条初始线索、第 0 天 |
| `assets` | 地图、立绘、音频及使用条件 | 道格拉斯插图、地点地图 |
| `normalization_decisions` | Agent 已自动解决的歧义、策略和来源 | 人数、腐臭、SAN 顺序等 6 项 |
| `validation` | Validator 版本、检查项和错误 | `passed`、无错误 |

## 2. `module`：模组身份与规则绑定

### 空框架

```json
{
  "module_id": "<stable-module-id>",
  "title": "<module-title>",
  "ruleset_ref": {
    "system_id": "<ruleset-id>",
    "version": "<ruleset-version>",
    "required_capabilities": [],
    "required_condition_types": [],
    "required_effect_types": []
  },
  "setting": {},
  "player_count": {},
  "premise": "<player-facing-premise>",
  "entry_scene_id": "<scene-id>"
}
```

### 《追书人》

```json
{
  "module_id": "paper-chase-zh-coc7",
  "title": "追书人",
  "ruleset_ref": {
    "system_id": "coc7",
    "version": "7e",
    "required_capabilities": ["skill_check", "sanity_check", "ending_resolution"]
  },
  "player_count": {
    "investigators_min": 1,
    "investigators_max": 1,
    "keeper_count": 1
  },
  "entry_scene_id": "scene.client_briefing"
}
```

Agent 负责识别模组用了哪些规则能力；骰点算法和技能定义由规则系统提供，不能复制进模组包。

## 3. `keeper_brief`：AI 主持总纲

### 空框架

```json
{
  "core_truth": "<keeper-only-core-truth>",
  "experience_goal": "<intended-player-experience>",
  "tone": [],
  "must_preserve": [],
  "must_not_reveal_before_granted": []
}
```

### 《追书人》

```json
{
  "core_truth": "道格拉斯主动选择进入地下世界并逐渐成为食尸鬼；他回来只是为了取回自己的藏书。",
  "experience_goal": "让调查员通过非线性调查发现真相，并决定是否尊重、隐瞒或暴力干预道格拉斯的选择。",
  "must_preserve": [
    "道格拉斯默认不主动攻击调查员",
    "玩家可以通过多条调查路径抵达墓地和最终对话",
    "暴力处理不是默认胜利路径"
  ],
  "must_not_reveal_before_granted": [
    "fact.douglas_became_ghoul",
    "fact.douglas_stole_books"
  ]
}
```

这部分防止 AI 主持虽然记住剧情，却把人物演错或过早剧透。

## 4. `content`：Agent 填充的八类核心对象

| 集合 | 每项至少需要 | 《追书人》示例 |
|---|---|---|
| `facts` | ID、秘密事实、可见性、来源 | 道格拉斯成为食尸鬼 |
| `scenes` | 玩家描述、Keeper 说明、实体/线索/检定/转场引用 | 金博尔宅、墓地、地穴等 12 个场景 |
| `entities` | 类型、名称、知识、目标、行为、初始状态、可选数值 | 道格拉斯、看守、日记、入口石板 |
| `clues` | 摘要、重要性、揭示 Fact、效果、来源 | 日记决定、蹄状足迹、入口线索 |
| `checkpoints` | 场景、技能、难度、前置、重复、成本和结果 | 搜索书房、夜间监视、移动石板 |
| `sanity_events` | 触发条件、成功/失败损失和上限 | 看见道格拉斯 `0/1D6` |
| `triggers` | 事件、条件、效果、优先级和执行次数 | 打开地穴后触发腐臭 |
| `endings` | 条件、优先级、结构化结果和摘要 | 和平解决、失踪、疗养院、被捕 |

### 4.1 Scene 对照

空 Scene：

```json
{
  "id": "<scene-id>",
  "name": "<scene-name>",
  "kind": "<scene-kind>",
  "summary": "<keeper-summary>",
  "player_description": "<safe-player-description>",
  "keeper_notes": "<keeper-only-guidance>",
  "entity_ids": [],
  "clue_ids": [],
  "checkpoint_ids": [],
  "trigger_ids": [],
  "next_scene_ids": [],
  "source_refs": []
}
```

《追书人》地穴场景：

```json
{
  "id": "scene.crypt",
  "name": "地穴入口",
  "kind": "hazard",
  "player_description": "异常足迹消失在一块沉重的墓穴石板旁，石板下方似乎连着黑暗空间。",
  "keeper_notes": "移开石板后触发腐臭；只有玩家提前声明屏住呼吸才能避免昏迷。",
  "entity_ids": ["location.crypt_entrance", "npc.douglas"],
  "checkpoint_ids": ["check.move_crypt_slab"],
  "trigger_ids": ["trigger.crypt_stench", "trigger.douglas_waits_at_crypt"],
  "next_scene_ids": ["scene.douglas_conversation"]
}
```

### 4.2 NPC 对照

空 NPC：

```json
{
  "id": "<npc-id>",
  "kind": "npc",
  "name": "<name>",
  "public_description": "<player-visible-description>",
  "keeper_description": "<keeper-only-description>",
  "knowledge_fact_ids": [],
  "knowledge_clue_ids": [],
  "goals": [],
  "behavior": {},
  "initial_state": {},
  "stat_block": null,
  "source_refs": []
}
```

《追书人》道格拉斯：

```json
{
  "id": "npc.douglas",
  "kind": "npc",
  "name": "道格拉斯·金博尔",
  "keeper_description": "已经成为食尸鬼，但保留理智、阅读爱好和人格。",
  "knowledge_fact_ids": [
    "fact.douglas_became_ghoul",
    "fact.ghouls_closing_entrance"
  ],
  "goals": ["取回自己的书", "不受人类社会干扰", "永久离开这处墓地"],
  "behavior": {
    "default_attitude": "non_hostile",
    "will_not_initiate_combat": true,
    "will_answer_polite_questions": true
  },
  "initial_state": {
    "location": "underground",
    "alive": true,
    "conversation_completed": false
  },
  "stat_block": {
    "STR": 85,
    "HP": 14,
    "fighting": 40,
    "san_loss_on_sight": "0/1D6"
  }
}
```

### 4.3 Checkpoint 对照

空 Checkpoint：

```json
{
  "id": "<check-id>",
  "scene_id": "<scene-id>",
  "skills": [],
  "difficulty": "<difficulty>",
  "prerequisites": [],
  "repeat": null,
  "time_cost": null,
  "on_success": [],
  "on_failure": [],
  "on_fumble": [],
  "source_refs": []
}
```

《追书人》搜索书房：

```json
{
  "id": "check.search_study",
  "scene_id": "scene.kimball_house",
  "skills": ["spot_hidden"],
  "difficulty": "regular",
  "time_cost": "1 day",
  "on_success": [
    {"type": "set_state", "path": "object.douglas_diary.found", "value": true},
    {"type": "grant_clue", "clue_id": "clue.diary_decision"}
  ]
}
```

### 4.4 Trigger 和 Ending 对照

《追书人》腐臭 Trigger：

```json
{
  "id": "trigger.crypt_stench",
  "event": "crypt_entrance_opened",
  "effects": [
    {
      "type": "conditional_hazard",
      "unless_player_declared": "hold_breath",
      "effect": "unconscious_until_night"
    }
  ]
}
```

《追书人》和平 Ending：

```json
{
  "id": "ending.peaceful_resolution",
  "priority": 100,
  "conditions": [
    {"type": "state_eq", "path": "npc.douglas.conversation_completed", "value": true}
  ],
  "outcome": "resolved",
  "summary": "调查员理解道格拉斯的选择并得知道格拉斯不会再回来。"
}
```

## 5. `initial_state`：从模板生成第一局状态

空框架只声明状态槽位，Agent 必须根据模组填入入口场景和初始线索：

| 字段 | 《追书人》 |
|---|---|
| `current_scene_id` | `scene.client_briefing` |
| `discovered_scene_ids` | 委托场景 |
| `granted_clue_ids` | 五本书失窃、道格拉斯失踪 |
| `completed_checkpoint_ids` | 空 |
| `fired_trigger_ids` | 空 |
| `active_ending_id` | `null` |
| `clock` | 第 0 天、白天 |
| `variables` | `books.disposition=undecided` |

Entity 的位置、存活和物品状态由各自 `initial_state` 合并进第一份 GameState。

## 6. Agent、规则系统和运行时的填充边界

| 内容 | Parser Agent | 规则系统 | 运行时 |
|---|---:|---:|---:|
| 从原文提取场景、NPC、线索和结局 | 是 | 否 | 否 |
| 生成稳定 ID 和对象引用 | 是 | 否 | 否 |
| 区分玩家信息和 Keeper 信息 | 是 | 否 | 执行过滤 |
| 将技能名映射到规范 ID | 是 | 提供目录并校验 | 否 |
| 定义骰点和成功等级算法 | 否 | 是 | 调用 |
| 生成 Checkpoint、Condition 和 Effect | 是 | 校验并执行 | 编排调用 |
| 生成模组初始状态 | 是 | 校验规则字段 | 创建实例 |
| 保存玩家行动后的状态 | 否 | 计算变化 | 是 |
| 自动解决原文歧义 | 是 | 提供规则依据 | 否 |
| 判断包是否可以发布 | 生成并修复 | 参与能力校验 | Validator/发布流程决定 |

## 7. 从空框架到成品

```text
module-package.template.json
  -> Agent 填入来源和模组身份
  -> 提取 Keeper 真相、场景、实体和线索
  -> 绑定规则系统并生成 Checkpoint/SAN/Trigger/Ending
  -> 生成 initial_state 和资源清单
  -> 自动解决歧义并记录 normalization_decisions
  -> Validator 检查并自动修复
  -> package_status=ready
  -> 写入 scenario_revisions.package_json
```

## 8. 本次框架评审结论

当前框架已经覆盖三类运行需求：

- AI 主持需要的叙事、知识边界、NPC 行为和防剧透字段；
- 规则引擎需要的检定、SAN、Condition、Effect、Trigger 和 Ending；
- 编排器需要的稳定引用、场景连接和初始状态。

《追书人》可以作为 Parser Agent 的目标输出和后续组件的黄金 Fixture。要在当前应用中真正开团，还需另外实现：

1. 将本框架固化为 JSON Schema 或 Pydantic Contract；
2. 实现 `ModulePackage Loader` 和首份 GameState 构建；
3. 让规则引擎注册并执行包中声明的 Condition/Effect；
4. 让 AI 主持按可见性检索 `keeper_brief`、Scene、NPC、Fact 和 Clue；
5. 加入关键线索可达性和模拟跑团测试。

这些是运行时接入工作，不再改变 Parser Agent 最终应产出的整体框架。

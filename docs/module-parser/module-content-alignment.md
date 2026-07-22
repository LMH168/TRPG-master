# ModulePackage 与 ModuleContent 字段对齐

状态：**讨论稿，待 A/B/C 共同确认**

对比对象：

- 模组解析侧：[`module-package.template.json`](module-package.template.json) 中的 `ModulePackage`；
- 协作框架侧：`agent-collaboration-framework/collaboration_framework/contracts/module.py` 中的 `ModuleContent`；
- 协作框架数据模型：[`数据模型设计.md`](https://github.com/1024XEngineer/TRPG-master/blob/agent-collaboration-framework/agent-collaboration-framework/docs/%E6%95%B0%E6%8D%AE%E6%A8%A1%E5%9E%8B%E8%AE%BE%E8%AE%A1.md)。

本文只做字段和职责对齐，不在本次文档变更中修改协作框架 Pydantic Contract。

## 1. 对齐结论

两份模型并非完全重复：

- `ModulePackage` 是 Parser Agent 的完整发布包，负责来源、版权、解析决策、素材、初始状态和验证；
- `ModuleContent` 是 B/C 共同维护的运行时模组语言，负责 Scene、Entity、Checkpoint 和规则声明；
- 两者在 `module_id`、版本、Scene、Entity、Checkpoint、规则结果和结局上存在直接重合。

建议最终形成包含关系：

```text
ModulePackage
├── 发布与审计字段
├── content: ModuleContent
├── initial_state
├── assets
└── validation
```

同一个概念只保留一个模型和一个名称。`ModuleContent` 的 Pydantic 模型作为 `content` 的事实源，ModulePackage 不再手写另一套 Scene、Entity 和 Checkpoint Schema。

## 2. 顶层字段对比

| ModulePackage | 当前 ModuleContent | 重合情况 | 建议归属 |
|---|---|---|---|
| `package_schema_version` | 无 | 新增 | Package，表示发布包协议版本 |
| `package_id` | 无 | 新增 | Package，表示发布包身份 |
| `package_status` | 无 | 新增 | Package，表示 `template/ready/failed` 等状态 |
| `source_manifest` | 无 | 新增 | Package，保存来源、分段、提取质量和权利 |
| `module.module_id` | `module_id` | 完全重复 | 只保留 `ModuleContent.module_id` |
| 无对应独立字段 | `version` | Package 中含义分散 | 保留 `ModuleContent.version` 表示模组内容版本 |
| 无对应独立字段 | `world_ref` | Package 仅有 setting 文本 | 保留 `ModuleContent.world_ref` |
| `module.ruleset_ref` | 无 | 新增 | 建议加入 ModuleContent，运行时必须读取 |
| `module.title/original_title` | 无 | 新增 | Package `metadata`，用于目录展示 |
| `module.setting` | `world_ref` 仅部分覆盖 | 部分重合 | `world_ref` 保持引用，时代/地点/主题放 Package metadata |
| `module.player_count` | 无 | 新增 | Package metadata；运行时创建房间时校验 |
| `module.estimated_duration` | 无 | 新增 | Package metadata |
| `module.character_setup` | 无 | 新增 | ModuleContent，影响开团和角色合法性 |
| `module.content_advisories` | 无 | 新增 | Package metadata，供产品展示和安全提示 |
| `module.entry_scene_id` | 无 | 新增 | ModuleContent，Loader 创建 GameState 必需 |
| `module.entry_points` | 无 | 新增 | ModuleContent，表达多个导入入口 |
| `keeper_brief` | 无 | 新增 | ModuleContent 私有主持约束，不进入 PlayerView |
| `runtime_defaults` | 无 | 新增 | ModuleContent，供 B 确定性执行缺省语义 |
| `content` | ModuleContent 本体 | 直接重合 | `ModulePackage.content: ModuleContent` |
| `initial_state` | Entity `state` 只能覆盖一部分 | 部分重合 | Package/Loader 使用的整局初始化模板 |
| `assets` | 无 | 新增 | Package，文件在对象存储，语义引用进入 Content |
| `normalization_decisions` | 无 | 新增 | Package，供 Parser 审计，不进入运行时 |
| `validation` | Pydantic 校验结果未入包 | 新增 | Package，保存发布门禁摘要 |

### 2.1 版本字段不要合并

以下字段名称相似，但语义不同，应同时存在：

| 字段 | 含义 |
|---|---|
| `package_schema_version` | ModulePackage 外层协议版本 |
| `content.version` | 具体模组内容版本 |
| `scenario_revisions.revision_number` | 数据库发布修订号 |
| `PlayerView.revision` | 当前玩家安全视图版本 |
| `GameState.state_version` | 某一局的运行时状态版本 |

## 3. 内容集合对比

当前 ModuleContent 只有 4 类集合，ModulePackage 有 16 类集合。

| 集合 | ModulePackage | 当前 ModuleContent | 建议 |
|---|---:|---:|---|
| `facts` | 有 | 无正式 `FactSpec`，仅有字符串 `facts` | 新增 `FactSpec`，所有引用使用 `fact_ids` |
| `scenes` | 有 | 有 | 复用并扩展现有 `SceneSpec` |
| `locations` | 有 | 混在 `EntitySpec(kind="location")` | 从 Entity 拆出 `LocationSpec` |
| `entities` | 有 | 有 | 复用并将范围收窄到 NPC、怪物和群体 |
| `characters` | 有 | 无 | 后续新增 `CharacterSpec` |
| `resources` | 有 | 混在 `EntitySpec(kind="object")` | 从 Entity 拆出 `ResourceSpec` |
| `clues` | 有 | 无 | 新增 `ClueSpec`，区分事实与获得事实的路径 |
| `checkpoints` | 有 | 有 | 复用并扩展现有 `CheckpointSpec` |
| `sanity_events` | 有 | 无 | 新增 `SanityEventSpec`，由 B 结算 |
| `timelines` | 有 | 无 | 后续新增 `TimelineSpec` |
| `tracks` | 有 | 无 | 后续新增 `TrackSpec` |
| `encounters` | 有 | 无 | 后续新增 `EncounterSpec` |
| `puzzles` | 有 | 无 | 后续新增 `PuzzleSpec` |
| `tables` | 有 | 无 | 后续新增 `TableSpec` |
| `triggers` | 有 | 有相近的 `RuleSpec` | 不保留两套，统一为 `rules`/`RuleSpec` |
| `endings` | 有 | `win_conditions` | 统一终局概念，建议使用语义更完整的 `endings` |

## 4. SceneSpec 字段对比

| ModulePackage Scene | 当前 SceneSpec | 关系 | 建议统一字段 |
|---|---|---|---|
| `id` | `id` | 相同 | `id` |
| `name` | `name` | 相同 | `name` |
| `kind` | 无 | 新增 | `kind` |
| `summary` | 无 | 新增 | 可选 `summary`，仅供主持快速检索 |
| `player_description` | `content` | 同义 | 只保留 `content` |
| `keeper_notes` | 无；Entity 已使用 `secrets` | 私有描述 | Scene 增加 `secrets`，不再使用 `keeper_notes` |
| `location_ids` | location 混在 `entity_ids` | 结构冲突 | 拆出 `location_ids` |
| `entity_ids` | `entity_ids` | 相同 | `entity_ids` |
| `resource_ids` | object 混在 `entity_ids` | 结构冲突 | 拆出 `resource_ids` |
| `clue_ids` | 无 | 新增 | `clue_ids` |
| `checkpoint_ids` | `checkpoint_ids` | 相同 | `checkpoint_ids` |
| `encounter_ids` | 无 | 新增 | `encounter_ids` |
| `trigger_ids` | Entity 内 `rules` | 重合 | Scene 关联 `rule_ids`，不再使用 `trigger_ids` |
| `next_scene_ids` | 无 | 新增 | `next_scene_ids` |
| `source_refs` | 无 | Parser 审计字段 | 保留在发布模型，运行时可忽略 |

建议结果：

```text
SceneSpec
  id / name / kind / summary
  content / secrets
  location_ids / entity_ids / resource_ids
  clue_ids / checkpoint_ids / encounter_ids / rule_ids
  next_scene_ids / source_refs
```

## 5. EntitySpec 字段对比

| ModulePackage Entity | 当前 EntitySpec | 关系 | 建议统一字段 |
|---|---|---|---|
| `id` | `id` | 相同 | `id` |
| `kind` | `kind` | 相同但取值不同 | `kind`，移除 `object/location` 后补充 `creature/group` |
| `name` | `name` | 相同 | `name` |
| 无 | `aliases` | 当前 Contract 已有 | `aliases` |
| `public_description` | `content` | 同义 | `content` |
| `keeper_description` | `secrets` | 同义 | `secrets` |
| `knowledge_fact_ids` | 无 | 新增 | `knowledge_fact_ids` |
| `knowledge_clue_ids` | 无 | 新增 | `knowledge_clue_ids` |
| `goals` | 无 | 新增 | `goals` |
| `behavior` | `refuse_ops/blocked_text/direct_responses` 部分覆盖 | 部分重合 | 保留现有明确字段，新增结构化 `behavior` 前需 A/B 评审 |
| `speech_style` | 无 | 新增 | `speech_style` |
| `initial_state` | `state` | 同义 | 只保留 `state` |
| `stat_block` | 无 | 新增 | `stat_block`，由 Ruleset 校验 |
| Trigger 引用 | `rules` | 当前已有 | `rules` |
| `source_refs` | 无 | Parser 审计字段 | 增加 `source_refs` |

`EntitySpec.state` 是实体初始状态；整局 `initial_state` 和运行时 `GameState` 与它不是同一概念。

## 6. CheckpointSpec 字段对比

| ModulePackage Checkpoint | 当前 CheckpointSpec | 关系 | 建议统一字段 |
|---|---|---|---|
| `id` | `id` | 相同 | `id` |
| `scene_id` | `scene_id` | 相同 | `scene_id` |
| 无 | `action` | 当前 Contract 已有 | `action` |
| 无 | `target_id` | 当前 Contract 已有 | `target_id` |
| `skills` | `skills` | 相同 | `skills` |
| `difficulty` | `difficulty` | 相同 | `difficulty` |
| `prerequisites` | 无 | 新增 | `prerequisites` |
| `repeat` | 无 | 新增 | `repeat` |
| `time_cost` | 无 | 新增 | `time_cost` |
| `on_success` | `outcomes.success` | 同义 | `outcomes.success` |
| `on_failure` | `outcomes.failure` | 同义 | `outcomes.failure` |
| `on_fumble` | 无 | 新增 | `outcomes.fumble` 可选 |
| `bypass_hints` | 无 | 新增 | 建议改为结构化 `bypass_conditions` |
| 无 | `mvp_check_result` | Fake 专用 | 从生产 Contract 移到测试配置 |
| `source_refs` | 无 | Parser 审计字段 | 增加 `source_refs` |

Outcome 内统一沿用现有名称：

```text
facts                -> 改为 fact_ids，避免文本与 ID 混用
player_visible_information
narration_constraints
ops
clue_ids             -> 新增
```

不再同时维护 `on_success` 和 `outcomes.success`，也不再同时维护 `effects` 和 `ops`。

## 7. Condition、Operation、Rule 与 Ending

### 7.1 Condition

当前 `ConditionSpec(path, equals)` 是最小可执行条件；ModulePackage 使用带 `type` 的多种 Condition。

建议以 Pydantic `ConditionSpec` 为事实源，后续按需要扩展 discriminated union，例如：

```text
StateEqualsCondition
ClueOwnedCondition
SceneIsCondition
PlayerChoiceCondition
CheckResultCondition
```

Parser Agent 只能生成 Contract 已注册的 Condition。

### 7.2 Operation

当前 Contract 使用 `OperationSpec` 和字段 `op`，ModulePackage 使用 Effect 和字段 `type`。

建议统一沿用：

```text
OperationSpec
op
ops
```

在现有 `allow/modify` 基础上逐步增加：

```text
grant_clue / move_entity / transition_scene / advance_time
request_sanity_check / start_encounter / trigger_ending / consume_resource
```

### 7.3 Rule 与 Trigger

`RuleSpec(hook, when, then)` 已经表达事件触发规则，与 `triggers` 高度重合。

建议：

- 保留 `RuleSpec` 和 `rules`；
- 扩展 `hook` 支持时间、轨道和遭遇事件；
- ModulePackage 不再维护独立 `TriggerSpec`；
- Scene、Entity、Timeline 等对象通过 `rule_ids` 或内嵌 `rules` 关联。

### 7.4 Ending 与 WinCondition

`win_conditions` 只能表达胜利，但模组还存在被捕、失踪、死亡和加入敌方等终局。

建议统一使用：

```text
EndingSpec
endings
```

这是对当前 `WinConditionSpec/win_conditions` 的破坏性升级，需要同步修改 B 的 Fixture、Schema 和测试，不能同时保留两套终局集合。

## 8. ModulePackage 新增字段汇总

### 8.1 只属于发布和解析审计

这些字段不应进入 AI 主持的 PlayerView，也不要求 B 在每回合消费：

| 字段 | 用途 |
|---|---|
| `package_schema_version` | 发布包协议兼容性 |
| `package_id/package_status` | 发布包身份与状态 |
| `source_manifest.segments` | 区分模组、附录、角色卡、手册和其他剧本 |
| `source_manifest.extraction_quality` | 文本、版面和素材提取质量 |
| `source_manifest.rights` | 商业使用与再分发状态 |
| `assets` | 地图、插图、音频等素材元数据和语义关联 |
| `normalization_decisions` | Parser 自动处理歧义的依据 |
| `validation` | 自动发布门禁摘要 |

### 8.2 需要进入 ModuleContent 的运行字段

| 字段/集合 | 使用方 |
|---|---|
| `ruleset_ref` | B 校验规则能力 |
| `entry_scene_id/entry_points` | Loader、B |
| `character_setup` | 开房流程、Loader |
| `keeper_brief` | A 的私有 Context Builder |
| `runtime_defaults` | B、编排器 |
| `facts/clues` | B 的安全事实投影、A 的防剧透叙事 |
| `locations/resources` | Loader、B、PlayerView 投影 |
| `sanity_events` | B |
| `timelines/tracks/encounters` | B、编排器 |
| `characters` | Loader、角色选择流程 |
| `puzzles/tables` | B；可以后续实现 |

### 8.3 只属于整局初始化

`initial_state` 不属于静态 Scene/Entity 定义，也不是正在运行的 GameState。Loader 用它创建每一局独立状态：

```text
current_scene_id
discovered_scene_ids
granted_clue_ids
completed_checkpoint_ids
fired_rule_ids
active_timeline_ids
track_states
inventory_resource_ids
active_encounter_id
active_ending_id
clock
variables
```

如果统一使用 `rules`，原来的 `fired_trigger_ids` 应同步改为 `fired_rule_ids`。

## 9. 建议目标结构

```text
ModulePackage
  package_schema_version
  package_id
  package_status
  source_manifest
  metadata
  content: ModuleContent
  initial_state
  assets
  normalization_decisions
  validation

ModuleContent
  module_id
  version
  world_ref
  ruleset_ref
  entry_scene_id
  entry_points
  character_setup
  keeper_brief
  runtime_defaults
  facts
  scenes
  locations
  entities
  characters
  resources
  clues
  checkpoints
  sanity_events
  timelines
  tracks
  encounters
  puzzles
  tables
  rules
  endings
```

`metadata` 只保存标题、原名、简介、时代、地点、人数、时长、主题和内容提示，不再重复 `module_id/version/world_ref/ruleset_ref`。

## 10. 分阶段对齐顺序

### P0：联调前必须完成

1. `ModulePackage.content` 直接使用 `ModuleContent`；
2. 统一 Scene 的 `content/secrets`；
3. 统一 Entity 的 `content/secrets/state`；
4. 统一 Checkpoint 的 `outcomes/ops`；
5. Condition 只使用 Pydantic 注册类型；
6. Effect 统一为 `OperationSpec`；
7. Trigger 统一为 `RuleSpec`；
8. `win_conditions` 与 `endings` 只保留一套；
9. 增加 `entry_scene_id` 和正式 `FactSpec`；
10. 将 `mvp_check_result` 移出生产 Contract。

### P1：完整跑通《追书人》需要

1. `ClueSpec`；
2. `LocationSpec`；
3. `ResourceSpec`；
4. `SanityEventSpec`；
5. `keeper_brief` 和知识边界；
6. Package `initial_state` 到 GameState 的 Loader；
7. PlayerView 增加安全的 Location、Clue 和 Resource 投影。

### P2：支持复杂模组时增加

```text
CharacterSpec
TimelineSpec
TrackSpec
EncounterSpec
PuzzleSpec
TableSpec
```

## 11. A/B/C 需要共同确认的决策

- [ ] `ModulePackage.content` 是否直接声明为 `ModuleContent`；
- [ ] Scene 是否统一为 `content/secrets`；
- [ ] Entity 是否拆出 Location 和 Resource；
- [ ] Checkpoint 是否统一使用 `outcomes/ops`；
- [ ] ModulePackage 的 Effect 是否全部迁移到 `OperationSpec`；
- [ ] `triggers` 是否全部迁移为 `rules/RuleSpec`；
- [ ] `win_conditions` 是否升级为 `endings/EndingSpec`；
- [ ] `facts` 是否改为正式 `FactSpec`，引用统一使用 `fact_ids`；
- [ ] 哪些新增集合进入 P0、P1 和 P2；
- [ ] ModuleContent Contract 版本如何升级，是否需要旧 Fixture 适配器；
- [ ] Parser、Loader、B 和 A 的端到端 Contract 测试由谁负责。

确认以上决策后，应先修改 `contracts/module.py`，再生成 JSON Schema、迁移 Fixture、更新 ModulePackage 模板和文档，避免继续维护两套手写协议。

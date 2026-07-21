# Module Parser Agent 产物契约

本文定义模组解析 Agent 的开发目标：把用户上传的模组自动转换为经过校验、可以被规则引擎、AI 主持和游戏编排器直接加载的 `ModulePackage`。

黄金样例：[`paper-chase/module-package.json`](paper-chase/module-package.json)

## 1. 核心结论

`ModuleDraft` 可以存在于 Agent 内部推理过程，但不是产品交付物。正式导入只有两种结果：

```text
ready  -> 输出可直接运行的 ModulePackage
failed -> 不创建可运行模组，返回结构化错误
```

玩家不负责审核解析结果。解析 Agent 必须通过自动归一化、交叉检查、规则校验和修复循环解决问题。无法安全解决的 blocker 应导致导入失败，不能把开放的 `review_items` 带入运行时。

## 2. 完整产物链

### 2.1 Agent 内部产物

以下内容用于追踪和调试，可以存入 `module_import_artifacts`，但不被游戏运行时加载：

| 产物 | 内容 |
|---|---|
| `SourceManifest` | 文件哈希、页码、语言、版权声明和提取方式 |
| `ExtractedDocument` | 文本块、章节、表格、图片位置和 OCR 信息 |
| `ModuleDraft` | 第一次结构化结果，可包含低置信度字段和待修复问题 |
| `ValidationReport` | Schema、引用、规则、可见性、可达性和循环检查结果 |
| `RepairTrace` | 自动修复前后差异、采用的策略和重试次数 |

### 2.2 唯一正式产物

`ModulePackage` 是唯一正式交付物，必须满足：

- `package_status = ready`；
- 不包含开放问题或未决占位符；
- 通过 Schema 和语义校验；
- 固定规则系统及版本；
- 具有完整初始状态；
- 可以直接创建一局游戏；
- 发布后不可原地修改，修改必须创建新 revision。

## 3. `ModulePackage` 顶层结构

```json
{
  "package_schema_version": "1.0.0",
  "package_id": "module-package.paper-chase-zh-coc7.v1",
  "package_status": "ready",
  "source_manifest": {},
  "module": {},
  "keeper_brief": {},
  "runtime_defaults": {},
  "content": {
    "facts": [],
    "scenes": [],
    "entities": [],
    "clues": [],
    "checkpoints": [],
    "sanity_events": [],
    "triggers": [],
    "endings": []
  },
  "initial_state": {},
  "assets": [],
  "normalization_decisions": [],
  "validation": {}
}
```

| 部分 | 必须包含什么 | 消费方 |
|---|---|---|
| `source_manifest` | 文件、页数、语言、提取方式；原文件不直接进入运行时 | 审计系统 |
| `module` | 标题、规则版本、设定、人数、简介、入口场景 | 产品、编排器 |
| `keeper_brief` | 核心真相、体验目标、基调、必须保持和禁止提前透露的内容 | AI 主持 |
| `runtime_defaults` | 缺省可见性、Trigger 和未声明结果的确定性语义 | 编排器、规则引擎 |
| `content` | 可执行的场景、实体、线索、检定、Trigger 和结局 | 全部运行时组件 |
| `initial_state` | 当前场景、初始线索、计时器、变量和已执行对象 | 编排器、规则引擎 |
| `assets` | 地图、立绘、音频及展示条件 | AI 主持、前端 |
| `normalization_decisions` | Agent 已经自动解决的原文歧义及依据 | 审计、调试 |
| `validation` | 自动门禁结果、验证器版本和错误列表 | 发布流程 |

## 4. AI 主持所需内容

### 4.1 `keeper_brief`

必须明确：

- `core_truth`：模组完整真相；
- `experience_goal`：希望玩家获得的核心体验；
- `tone`：叙事基调；
- `must_preserve`：AI 不能违反的剧情和人物约束；
- `must_not_reveal_before_granted`：不能提前透露的 Fact ID。

### 4.2 Scene

每个 Scene 至少包含：

```text
id
name
kind
player_description
keeper_notes
entity_ids
clue_ids
checkpoint_ids
trigger_ids
next_scene_ids
source_refs
```

`player_description` 可以直接进入玩家上下文；`keeper_notes` 只能进入 AI 主持私有上下文。

### 4.3 NPC Entity

NPC 至少应包含：

```text
public_description / keeper_description
knowledge_fact_ids / knowledge_clue_ids
goals
behavior.default_attitude
behavior.will_not_initiate_combat
behavior.conversation_constraints
speech_style
initial_state
stat_block（需要规则判定时）
```

知识边界用于防止 NPC 说出自己不可能知道的事实；行为约束用于防止 AI 为推动剧情而改变人物动机。

### 4.4 Fact 和 Clue

Fact 保存真实世界状态及可见性；Clue 保存玩家如何获得事实。Clue 至少需要：

```text
id
summary 或 player_facing_text
criticality
reveals_fact_ids
effects
source_refs
```

如果 Clue 没有单独声明可见性，必须由 `runtime_defaults.clue_visibility` 给出确定性规则。

## 5. 规则引擎所需内容

### 5.1 规则绑定

`module.ruleset_ref` 必须固定：

```text
system_id
version
required_capabilities
```

模组引用规则引擎已有能力，不能在模组中重新定义骰点和成功等级算法。技能、属性、Condition 和 Effect 必须使用规则系统注册的规范 ID。

### 5.2 Checkpoint

每个检定至少包含：

```json
{
  "id": "check.search_study",
  "scene_id": "scene.kimball_house",
  "skills": ["spot_hidden"],
  "difficulty": "regular",
  "prerequisites": [],
  "repeat": null,
  "time_cost": "1 day",
  "on_success": [
    {"type": "grant_clue", "clue_id": "clue.diary_decision"}
  ],
  "on_failure": [],
  "on_fumble": []
}
```

某个结果未声明时，必须由 `runtime_defaults.missing_checkpoint_outcome` 定义，例如 `no_effect`，不能交给 AI 临场补写。

### 5.3 SanityEvent、Trigger 和 Ending

- `SanityEvent`：触发条件、成功/失败损失、累计上限和附加效果；
- `Trigger`：事件、Condition、优先级、是否仅执行一次和 Effect；
- `Ending`：条件、优先级、结构化结果和叙事摘要。

Condition 和 Effect 使用受控类型，例如：

```text
Condition：state_eq、clue_owned、scene_is、player_choice、check_result
Effect：set_state、grant_clue、request_check、request_san_check、damage、transition、trigger_ending
```

## 6. 初始状态

`initial_state` 必须足以直接创建游戏实例：

```text
current_scene_id
discovered_scene_ids
granted_clue_ids
completed_checkpoint_ids
fired_trigger_ids
active_ending_id
clock
variables
```

实体自身的初始位置、存活状态、合作态度和物品状态保存在对应 `Entity.initial_state` 中，由 Loader 合并成第一份 GameState。

## 7. 自动处理歧义

解析 Agent 按以下优先级自动做决定：

1. **明确原文优先**：原文有确定描述时不做产品层改写；
2. **规则系统优先**：规则名、公式和难度映射到指定 Ruleset 的规范 ID；
3. **保持作者意图**：结局和人物动机优先保持原模组体验；
4. **保护玩家选择**：原文未规定唯一结果时，保留为运行时变量或玩家选择；
5. **避免无依据推断**：地图距离、隐藏关系等无法证明的信息不自动生成；
6. **保守降级**：非关键装饰信息不确定时可以省略，不得影响主线；
7. **失败而非猜测**：规则版本、核心结局或关键线索无法确定时，导入状态设为 `failed`。

所有自动决策写入 `normalization_decisions`，每项必须有 `status=resolved`、决策结果、策略和来源。`ready` 包中禁止出现 `review_items`、`TODO` 或 unresolved 决策。

## 8. 自动解析流水线

```text
上传文件
  -> 文本/OCR/版面提取
  -> 结构识别和来源绑定
  -> 生成 ModuleDraft
  -> 自动归一化规则和可见性
  -> Schema 与语义校验
  -> 自动修复并重新校验（限制次数）
  -> ready ModulePackage 或 failed ImportJob
```

玩家只看到成功导入的模组，或者“导入失败及原因”；不会被要求逐项审核解析内容。

## 9. 谁调用规则引擎

```text
玩家行动
  -> AI 主持提出 action/checkpoint_id
  -> 编排器检查当前场景、权限和 prerequisites
  -> 规则引擎执行检定、Condition 和 Effect
  -> 状态提交
  -> AI 主持根据结构化结果生成叙事
```

SanityEvent、Trigger、战斗效果和结局检查由编排器自动调用规则引擎。纯叙事对话无需调用。AI 主持不能修改骰点、绕过前置条件或自行写入状态。

## 10. 自动发布门禁

一个包只有通过以下检查才能标记为 `ready`：

- [ ] 顶层结构符合 `ModulePackage` Schema；
- [ ] 所有对象 ID 唯一且所有引用存在；
- [ ] 入口场景和初始状态有效；
- [ ] 玩家描述不包含未解锁 Keeper Fact；
- [ ] NPC 具有知识边界和关键行为约束；
- [ ] 技能、难度、Condition 和 Effect 被目标 Ruleset 支持；
- [ ] 核心线索至少有一条可达路径；
- [ ] Trigger 不存在无限循环；
- [ ] Ending 条件和优先级可以确定性结算；
- [ ] 所有 `normalization_decisions` 已解决；
- [ ] 包内不存在 `review_items`、TODO 或 unresolved 占位符；
- [ ] Validator 输出 `validation.status=passed` 且 `errors=[]`。

## 11. Parser Agent 的开发完成标准

针对任意受支持格式的模组，Parser Agent 应能够：

1. 生成与黄金样例相同结构的 `ModulePackage`；
2. 让 Loader 无需读取原文即可创建初始 GameState；
3. 让 AI 主持只凭包内容完成场景描述、NPC 对话和信息控制；
4. 让规则引擎只凭包内容发起检定、结算效果和判断结局；
5. 对无法安全解析的模组返回结构化失败，而不是输出半可用模组；
6. 保存来源和自动决策，使问题可以由开发者复现和调试。

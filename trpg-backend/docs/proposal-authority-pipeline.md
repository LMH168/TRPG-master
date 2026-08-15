<!-- 本文说明 Issue #10 Proposal-only Host 与 Engine 单一权威入口的迁移、恢复和运维约束。 -->

# Proposal-only Host 权威流水线

## 目标

Issue #10 将动作处理拆成两个边界：Host 只生成不带授权字段的
`HostDecisionProposal`，Engine 在当前事务内绑定可信身份、校验 revision、编译为内部
`ValidatedActionCommand`，然后原子提交状态、DomainEvent、执行结果和 receipt。

```text
玩家输入 → PlayerView → Host Proposal
                         ↓
                 Engine 事务内 Validator
                         ↓
                 ValidatedActionCommand
                         ↓
                 状态 + Event + receipt
```

Proposal 不得包含 `room_id`、`player_id`、`actor_id`、`request_id`、revision、权限等级、骰点
结果、提交状态或持久化意图。`persistence_intent` 只属于旧 ActionAdjudication 的兼容字段，
不能被 Host 用来授权结果。Proposal v2 的持久结果只由结构化完成条件与实际 Effect 决定；
开放字符串 `method_family` 仅用于表达方式和规则匹配，不能推导目标是否完成。

## 目标完成语义

生产 Proposal 使用 schema v2，并把 Coordinator 冻结的玩家原话或计划步骤目标作为
`requested_goal`。Host 输出的 `semantic_goal` 必须与它完全一致，不能把“打死守墓人”缩减成
“射击守墓人”。Host 同时声明以下一种 `GoalCompletionProposal`：

- `process`：观察、交谈等不要求持久状态变化的过程目标；
- `effects`：目标成立必须满足的角色状态、物品 condition、custody、地点、信息或消耗后置条件。

检定分支的 `outcome=success` 只说明检定或规则分支成功，不等于完整玩家目标已经完成。Engine
提交后根据最终 GameState 核对完成条件，生成 `goal_outcome`：`achieved`、
`partially_achieved`、`not_achieved`、`cancelled` 或 `legacy_unknown`。Narrator 只有在
`goal_outcome=achieved` 且 `committed_results`/最终 PlayerView 提供证据时，才能把持久目标描述为
已经完成；模型连续输出不合规内容时使用确定性兜底，不能从玩家原话补写伤势、死亡或物品变化。

## COC 轻量 AI 裁决

本阶段不引入完整战斗系统。Host 可以对当前场景中玩家可见的 NPC 提出受控结果，但 Engine
保留最终权威：

- `dead`、`unconscious`、重伤等高影响状态必须经过检定，且只能由成功分支提交；
- Host 提出的死亡完成条件必须有完全匹配的成功 Effect，规则托管动作则以 Module Rule 的实际
  Effect 为准；规则只证明命中时，死亡目标保持 `not_achieved`，不得补造死亡或重复掷骰；
- dead 状态不能被普通 Host Proposal 改回 conscious，复活必须来自明确 Module Rule；
- dead/unconscious NPC 不能参与 social 交互，Engine 在掷骰前返回玩家安全说明；观察尸体、搜查
  和物理处理仍可继续；
- hidden/keeper 实体、剧情事实、结局及 L4/L5 Effect 仍只能由 Module Rule 授权。

物品状态同样由 Engine 提交。`change_item_condition` 原子更新 ItemInstance condition、版本和
DomainEvent；丢弃只允许作用于当前角色实际持有的实例，目标地点取事务内当前地点。提交后最终
PlayerView 必须从 inventory 移除该物品，并在场景 `loose_items` 中显示同一实例及 condition。
已经满足的死亡或 custody 后置条件按幂等结果处理，不重复写状态事件。

复合计划的当前步骤若为 `partially_achieved` 或 `not_achieved`，计划立即停止，后续步骤保持
`pending`。玩家提示必须区分此前已完成、当前未完成和尚未执行的步骤；继续时提交一个新行动，
系统不能静默跳过失败步骤后执行旧计划余项。

## 生产入口与历史兼容

PR3 后生产动作只有 `submit_proposal`。Host、Controller 和 Narrator 不持有
`GameState` 或 `DomainEvent` 写端口，也不能直接构造或提交 `ValidatedActionCommand`。
Proposal 的可信身份、当前 revision、权限分类和规则所有权只能由 Engine 在权威事务内绑定。

旧的 `ActionAdjudication`、`SubmitAdjudicationRequest` 和 ActionPlan v1 JSON 仅作为历史
reader/adopter 保留。它们不能重新成为生产 writer：有已有 execution 或 receipt 的步骤只按
请求 ID 对账；没有 receipt 的旧未提交步骤进入 `needs_clarification`，等待玩家用新输入产生
Proposal。历史终态不会伪造回填新的 Turn、receipt 或 Proposal。

PR2 期间的 `legacy|shadow|v2` 临时模式开关已在 PR3 删除。已经进入 PR3 生产链路的动作不能
通过切换开关绕过 Validator；需要回滚时只能停止新动作并处理没有 Engine receipt 的新回合。

## 动态内容

动态引用使用 `runtime_entity` / `runtime_location` 逻辑 ID，不在场景或 Prompt 中硬编码
数据库随机 ID。Engine 使用 `room_id + request_id + logical ref` 的稳定摘要派生实际 ID，
并要求同一 Effect 序列保持以下顺序：

- 地点：`ensure_runtime_location → enter_location`；
- 普通物品：`ensure_runtime_entity → move_entity(self_inventory)`；
- 同行实体：按同一提交中的 `move_entity` 逐个移动到目标地点。

任一 Effect 校验或应用失败，整条命令回滚；恢复时先查询 receipt，不重新生成 Proposal、掷骰
或执行已提交 Effect。基线报告可以将派生 ID 规范化为场景逻辑别名，但这只影响报告，不影响
权威状态。

## 检定与恢复

Engine 返回 `awaiting_skill_choice` 或 `awaiting_post_roll_decision` 时，ActionPlanRun 保存
步骤 Proposal 和 Engine execution。玩家选择与 post-roll 决定仍通过 Engine 的现有权威接口，
进程重启后由 decision/check ID、owner、revision 和 receipt 重新对账。Narrator 失败只能停在
`awaiting_narration`，不能重新执行 Engine。

Validator 拒绝且尚无 receipt 时，可以在冻结的修复预算内基于最新 PlayerView 重新生成 Proposal；
修复必须保持原 `semantic_goal`。权限、规则所有权和持久结果完整性失败时进入玩家安全澄清，
不得把内部错误、Prompt、模型原始输出或隐藏上下文发送给玩家。

当前 writer 和 reader 的持久化版本如下：

| 载荷 | 当前 writer | 兼容 reader |
| --- | --- | --- |
| Proposal | v2 | v1/v2 |
| ValidatedActionCommand | v2 | v1/v2 |
| ActionPlanRun | v3 | v1/v2/v3 |
| PendingCheckDecision | v3 | v1/v2/v3 |
| CheckRun | v4 | v1/v2/v3/v4 |
| adjudication command | request v3 / result v5 | request v1-v3 / result v1-v5 |

旧终态记录保持原样，不伪造 GoalCompletion。旧非终态记录若已有 execution 或 receipt，只按原命令
对账并使用 `legacy_unknown`；没有权威提交证明时进入玩家安全澄清，要求重新提交行动。恢复不得
重新生成 Proposal、重掷骰或重复应用 Effect。

## 依赖边界

- Host 只读取 `PlayerView`、`KeeperCapabilityView` 和历史安全上下文，并输出 Proposal 或
  Clarification。
- Controller 只协调身份、Turn 和玩家安全结果；它不接收 Engine 写端口以外的状态写对象。
- Narrator 只接收已提交结果、证据和最终 PlayerView，不能写 GameState、DomainEvent、receipt
  或 Outbox。
- Engine 是唯一可以编译并提交 `ValidatedActionCommand` 的组件；receipt 是提交是否发生的
  唯一证明。

## 检查与回滚

PR2 的最小检查：

```bash
cd trpg-backend
uv run pytest tests/runtime_baseline -q
uv run pytest tests/test_action_plan_turn_recovery.py tests/test_action_plan_persistence.py -q
uv run pytest tests/test_adjudication_persistence.py tests/test_engine_persistence.py -q
uv run ruff check .
uv run ruff format --check .
```

回滚前先停止新动作并按 Turn 查询未完成回合。没有 receipt 的新回合可以通过可审查的代码
回退恢复；已经提交的 Proposal 回合必须继续使用 receipt 恢复。不得删除动态对象、事件或 receipt 来“回滚”
业务状态，也不得 force push 已进入 Review 的分支历史。

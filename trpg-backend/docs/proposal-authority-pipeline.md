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
Engine 根据开放 `method_family` 和实际 Effect 派生它，不能被 Host 用来授权结果。

## 生产模式

`AUTHORITY_PIPELINE_MODE` 支持短期 `legacy`、`shadow` 和 `v2`：

- `v2` 是 PR2 的默认模式。单动作、ActionPlan 当前步骤和修复后的动作均调用
  `submit_proposal`。
- `legacy` 仅供人工回滚尚未产生 Engine receipt 的新回合；已有 receipt 的回合必须按 receipt
  对账和恢复，不能切换路径重新执行。
- `shadow` 只能运行无副作用的 Proposal 编译比较，不掷骰、不写状态、不生成叙事或 Outbox。

PR3 完成后删除该临时开关和 legacy 生产 writer，但历史 JSON reader 继续保留。

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

回滚前先停止新动作并按 Turn 查询未完成回合。没有 receipt 的新回合可以人工切回 `legacy`；
已经提交的 v2 回合必须继续使用 receipt 恢复。不得删除动态对象、事件或 receipt 来“回滚”
业务状态，也不得 force push 已进入 Review 的分支历史。

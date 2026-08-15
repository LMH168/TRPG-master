# AI 主持运行时 v2 行为与故障基线

本文档说明 Issue #4 建立的确定性回放、故障注入、指标门槛和真实模型评测入口。该基线用于比较架构升级前后的行为；Issue #6 已将生产路径切换到 TurnRecord、receipt 与 Outbox，协议和恢复规则见 [`reliable-turn-protocol.md`](reliable-turn-protocol.md)。

## 快速运行

在 `trpg-backend` 目录执行：

```bash
uv run pytest tests/runtime_baseline -q
uv run python scripts/run_runtime_baseline.py
```

第二条命令将 JSON 报告写到标准输出，运行时日志写到标准错误。需要保存报告时使用：

```bash
uv run python scripts/run_runtime_baseline.py --output /tmp/runtime-baseline.json
```

默认路径只使用确定性 Fake、固定骰子序列和内存 Engine，不读取真实模型凭据，不发起模型网络请求。

## 场景契约

场景存放在 `tests/runtime_baseline/scenarios/`，当前 `schema_version` 固定为 `1`。每个场景包含：

- `id`：稳定且唯一的场景标识；
- `category`：对话、规则、持久状态、动态内容、叙事纹理、幂等或故障恢复；
- `initial_state`：模组、逻辑别名和允许调整的最小初始状态；
- `turns`：玩家输入、`client_action_id`、受控 Host/Narrator 输出和重复次数；
- `faults`：故障点、提交前后时机和触发次数；
- `expectation`：终态、事件、权威状态、叙事证据、禁止声明和已知缺口。

场景通过 `BaselineScenario` 严格校验，未知字段会失败。逻辑别名以 `@` 开头，由执行器在运行前解析为真实实体 ID；场景不得硬编码随机数据库 UUID。

示例：

```json
{
  "schema_version": 1,
  "id": "idempotency.action-retry",
  "category": "idempotency",
  "description": "同一动作重试不得重复写入状态。",
  "initial_state": {"module": "paper-chase"},
  "turns": [
    {
      "client_action_id": "baseline-idempotent-action",
      "utterance": "推开房门进入街道",
      "repeat": 2,
      "host_output": {
        "target": {"kind": "location", "id": "arnoldsburg_streets"},
        "method": {"family": "travel", "description": "进入街道"},
        "persistence_intent": "location",
        "success_effects": [
          {"type": "enter_location", "location_id": "arnoldsburg_streets"}
        ]
      }
    }
  ],
  "expectation": {
    "terminal_statuses": ["completed"],
    "required_state": {"scene_id": "arnoldsburg_streets"}
  }
}
```

## 执行与断言原则

`InMemoryRuntimeAdapter` 通过当前生产使用的 `ActionPlanTurnApplication`、`AdjudicationEngineService`、`RuleEngineService` 和 `ActionPlanNarrator` 执行场景。每个场景都会从发布的《追书人》ModuleContent v3 重建独立状态。

执行结果只保留稳定字段：阶段、事件类型、关键状态、证据引用以及按动作和序号规范化的事件/掷骰标识。时间戳、随机 UUID 和完整自然语言不进入快照。

强制不变量包括：

- 同一输入不得重复掷骰、DomainEvent 或状态版本；
- 持久状态必须与 Engine 已提交事件一致；
- Narrator 不得声明没有提交证据的持久事实；
- 提交后故障不得撤销或重复执行状态；
- 玩家不可见信息不得进入玩家侧结果。

自然语言只检查必要证据和禁止声明，不逐字比较措辞。

## 故障注入

故障代理仅存在于 `tests/runtime_baseline/`，不会进入生产依赖图。当前覆盖：

| 故障点 | 验证内容 |
| --- | --- |
| `host.before` | Host 调用前失败保持零提交，重试同一输入 |
| `validator.before` | 非法目标被拒绝，修正后重新裁决 |
| `engine.before` | 提交前失败可以安全重试 |
| `engine.after` | 提交后异常通过状态对账继续 |
| `narrator.before` | Engine 结果保留，只重试叙事 |
| `websocket.after` | 发送异常只重发同一个已生成结果 |
| `process.after` | Engine 提交后重建 Application，再按原动作 ID 恢复 |

`process.after` 场景验证应用依赖重建后复用同一权威 Store；SQL 文件数据库跨 Store/Service 重建由 `tests/test_action_plan_persistence.py` 的持久化恢复用例继续守护。WebSocket 代理验证投递边界，完整 Turn 协议、Outbox 稳定重放和 REST 恢复由 `tests/test_turn_runtime.py`、`tests/test_turn_outbox.py`、`tests/test_turn_api.py` 与 `tests/test_ws.py` 共同守护。

## 指标与不可恶化门槛

`baseline-thresholds.json` 冻结以下指标上限：

- 无明确终态的回合数；
- 各阶段注入错误数；
- 重复掷骰、事件和状态修改数；
- 状态与叙事不一致数；
- 提交状态不可判定数；
- 已知缺口数量。

同时冻结场景成功率下限。指标下降表示改善，可以直接通过；指标上升表示回归，CI 失败。增加场景数量不会触发回归，但新场景必须满足强制不变量。

阈值不得为了让失败变绿而随意提高。确需提高时，PR 必须说明：新增了什么场景、为什么现有架构无法满足、对应后续 Issue，以及为什么没有削弱权威性、安全性或隐私门禁。

当前冻结的已知缺口数量为 `0`。PR2 已将普通动态物品创建并拾取、动态地点创建并进入接入
Proposal 权威流水线；后续改动不得让这两类场景退回澄清或部分提交。除明确列入
`known_gap_assertions` 的断言外，同场景中的其他失败仍是硬失败。

## 真实模型评测

真实模型评测必须显式执行：

```bash
uv run python scripts/run_runtime_baseline.py --real-model
```

该命令设置 `RUN_REAL_MODEL_PLAY_SIM=1`，调用现有 `tests/test_play_sim_real_model.py`。报告仅记录 provider、model、测试文件、耗时、退出码和通过状态，不写入 API key、模型正文或玩家会话。

真实模型评测会产生网络请求和可能的模型费用。它用于观察意图理解、自由行动和叙事表现，不替代确定性门禁，也不在默认 CI 中执行。

## 数据脱敏规则

- 只使用仓库内发布模组和人工构造输入；
- 不提交原始玩家会话、账号、房间码、邮箱、IP 或外部请求内容；
- 不提交 API key、Token、Cookie、Authorization header 或 `.env` 内容；
- 真实模型 transcript 只写临时目录，不进入 Git；
- `contains_private_data` 必须为 `false`，否则场景加载失败。

## 扩展方式

后续架构 Issue 应复用现有契约和执行器：

1. 为新增或修复行为添加最小场景；
2. 使用结构化状态、事件和证据断言，不快照完整叙事；
3. 修复已知缺口时删除对应 `known_gap_assertions`，并降低阈值；
4. 新协议进入影子运行时，让旧链和新链执行同一场景并比较规范化结果；
5. 旧链删除后继续保留场景，避免替换式升级丢失历史行为门禁。

## 完整验证

```bash
uv run pytest tests/runtime_baseline -q
uv run pytest \
  tests/test_action_plan_turn_recovery.py \
  tests/test_action_plan_persistence.py \
  tests/test_engine_persistence.py \
  tests/test_ws.py -q
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest
```

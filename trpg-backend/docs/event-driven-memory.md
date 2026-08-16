# 事件驱动跨回合记忆

长期 Memory 是可靠 Turn、Engine receipt、公开 DomainEvent 和玩家安全交流的可重建读模型，
不是第二个状态写入口。生产模型输入的优先级固定为：

```text
当前 PlayerView
> 本回合 committed results / DomainEvent
> MemoryContext
> RecentTurnContext
> published narration
> 玩家主张
```

因此旧对话不能让死亡 NPC 继续回应，“曾经持有”不能覆盖当前物品 custody，旧地点记录也
不能覆盖当前场景。`heard` 和 `asserted` 只证明角色听过或说过；`presentation_only` 只用于
承接已经发布的表达，均不得提升为世界事实。

## 生产读取

Host 与 Narrator 默认读取最多 8 条、4000 字符的 `MemoryContext`。可信 `room_id`、
`viewer_player_id`、`viewer_actor_id`、PlayerView revision、当前位置和可见实体全部由服务端
绑定。排序依次考虑当前可见主体、当前地点、来源顺序和稳定 Memory ID。

Host Agent 的 `search_memories` 工具只接受查询文本、Memory kind、当前可见实体 ID 和返回
条数。工具不能指定房间、玩家或角色；不可见实体、其他玩家私有记忆和 superseded 记录不会
进入结果。显式文本搜索不受预载候选窗口限制，未命中时返回空列表，不进行语义猜测。

`RecentTurnContext` 继续保留相邻回合和当前场景的短期历史，用于指代、对白承接和重复抑制；
跨场景召回由 Memory 承担，避免两份历史重复消耗 token。

## 投影与重建

增量投影由 `MemoryProjectionSupervisor` 独立运行，失败不回滚 Turn，也不阻塞 Narration Outbox。
指定房间可执行：

```bash
cd trpg-backend
uv run python scripts/rebuild_memory_projection.py --room-id <room-id>
```

重建与增量处理复用同一投影函数和稳定 Memory ID。重复执行不会生成重复条目；缺少可靠 Turn、
receipt、参与者或可见性证据的旧记录会被跳过。

## 故障与回滚

Memory 读取、作用域校验或投影暂时失败时，模型接收同 revision 的空 `MemoryContext`，权威回合
继续执行。日志只记录错误类型和确定性计数，不记录 Prompt、模型原始输出或玩家私密内容。

回滚 Context 接入时移除 Host/Narrator 的 Memory Store 组合即可；`memory_entries` 与
`memory_projection_runs` 保持为可重建派生数据，不撤销 Engine 状态、receipt、Turn、Event 或
Outbox。停止投影 Supervisor 不影响现有游戏提交与断线恢复。

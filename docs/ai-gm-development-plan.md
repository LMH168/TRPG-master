<!-- 本文件把 AI 主持架构拆成可依赖、可验收的开发任务；先搭建可运行空骨架，再按垂直切片替换空实现。 -->
# AI 主持开发计划

- 状态：Ready，优先完成四个预设模组；公开分发授权按模组单独审核
- 架构依据：[AI 主持人系统设计报告](./ai-gm-system-design.md)
- 原则：按可运行垂直切片推进，不按目录或抽象层批量造空壳

## 1. 已确认的产品决策

| 决策 | 最终约定 | 影响 |
| --- | --- | --- |
| 房主是否能看 KP 秘密 | 不能；房主也是玩家，`keeper` 仅供系统内部和开发审计 | 房主使用普通玩家投影，不能调用秘密接口 |
| 角色卡何时进入运行状态 | 开局时从现有 `Character` 创建 Actor（局内行动实体）快照；游戏中角色卡只读，变化只写 Actor | Character/Actor 边界、重连和防作弊 |
| 玩家如何掷骰 | 玩家点击投骰，服务端用 `check_id` 只生成一次骰点；客户端不能填写骰点 | 保留玩家亲手投骰的交互，同时保证权威性和幂等 |
| 旧房间如何处理 | 已于 2026-08-19 清空本机 `app.db` 的旧房间及局内数据；账号与用户角色卡库已核对保留 | 不再安排迁移或二次清理，不编写旧对局兼容层 |
| 哪些内容可以提交 GitHub | 公开代码、Schema、工具和原创测试数据；预设运行包按各自许可决定 | 预设开发不被上传/解析流程阻塞；公开发布时保留作者、来源和许可信息 |

`Actor` 可直接理解为“局内行动实体”，包括玩家调查员、NPC 和怪物。玩家的 `Character` 是开局前可编辑、可复用的角色卡；每次开局，系统从 Character 复制一份只属于当前房间的玩家 Actor。此后 HP、SAN、位置、物品和状态只在 Actor 上变化，结团也不自动覆盖原角色卡，避免玩家通过修改角色卡改写正在进行的对局。

## 1.1 GitHub 与模组授权边界

首阶段目标是把四个预设模组做成可运行、可验收的 ModulePack，不处理用户上传和自动解析。GitHub 上可以公开源代码、Schema、工具和原创/合成测试数据；预设选择页应展示作者、来源链接和许可说明。公开发布原文或结构化改编包时，再按每份模组的许可单独确认：

- 明确允许原样转载的文件，满足署名和其他附加条件后才可公开原文件；
- 标注“禁止修改后发布”或“修改需联系作者”的模组，其结构化 ModulePack 属于改编产物，取得许可前不进入公开仓库；
- 未确认可公开分发的原文或改编包，不进入公开仓库或公共下载渠道，但不阻塞本地预设开发和测试；
- 正式上线前在 manifest 和产品页面记录作者、来源、许可和修改授权。

以下事项不阻塞开工：多人窗口超时值、100 Turn 的具体模型、语音表现、高级车辆追逐。它们在首次进入对应 Phase 时再定。

## 2. 全局完成定义

任何任务只有同时满足以下条件才算完成：

1. 权威状态变化只经过 Kernel 命令事务。
2. 新增非平凡逻辑至少有一个能失败的自动化测试。
3. 模型输出通过 Pydantic 和策略校验后才进入应用逻辑。
4. 玩家投影测试证明不含 `keeper/private(other)` 数据。
5. 重试、重连和进程恢复不会重复掷骰、消耗物品或触发事件。
6. SDK、前端和数据库契约在同一个变更中同步。

## 2.0 开发可审查性规范

所有新增或修改的代码必须满足以下要求：

- 关键逻辑使用中文注释，解释为什么这样做、边界条件和与 Kernel/权限/恢复契约的关系；
- 每个函数、方法和异步任务都在定义处写中文文档字符串，说明职责、输入、输出和失败行为；简单 getter/setter 也要保持可读命名，复杂分支不能只依赖函数名猜测；
- 系统提示词、Agent 指令、结构化字段说明和模型错误提示默认使用中文；技术标识符、协议枚举和 API 字段名保留稳定英文名称；
- 测试名称、测试注释和失败断言优先使用中文，确保 Review 时能直接看懂业务意图；
- 不允许把关键规则藏在无注释的 Prompt、魔法数字或隐式 fallback 中；必要的产品默认策略必须在代码和 ModulePack 中标注来源。

代码 Review 以“另一位开发者无需打开模组原文或猜测模型意图即可理解实现”为最低可读性标准。注释必须解释决策和约束，不写重复代码表面行为的空话。

## 2.1 开发方式：先搭骨架，再填能力

每个阶段都必须保留一条可以启动、请求、持久化、恢复和返回结果的最短链路。空骨架只允许暂时返回明确的 `not_implemented`，不能伪装成成功，也不能在后续阶段继续保留绕过 Kernel 的旁路。

```text
可启动应用
  -> 健康检查
  -> 创建新房间并冻结 ModulePack 版本
  -> 创建 Actor 快照
  -> 接收一条 Turn 输入
  -> 测试环境由 ScriptedModel 生成结构化 Proposal
  -> Kernel 返回 CommandResult（首版可只支持 Wait）
  -> 写事件、回执和 Outbox
  -> 返回玩家投影
```

每个阶段按同一顺序交付：

1. 先写契约和失败测试；
2. 再接数据库和 API 的最小实现；
3. 再接真实模型或新的规则能力；
4. 最后删除该阶段不再需要的假实现，并跑完整回归。

阶段之间不得同时改动多个未验证的领域：例如没有通过 Kernel 的移动测试前，不接真实模型；没有通过单人权限测试前，不扩展多人投影。

## 2.2 内容在服务器上的最小闭环

四个预设模组优先使用仓库内人工制作、人工校对的 ModulePack。首版不实现用户上传、PDF/DOCX 自动解析或通用内容管理平台：

```text
安装仓库内预设
  -> 安装人工校对的 ModulePack
  -> 房间创建时冻结 module_id/version/hash
  -> Context Builder 按权限和场景读取 ModulePack 切片
  -> 模型只收到当前职责所需的结构化数据和短片段
```

ModulePack 才是当前运行时契约。原始文件溯源、用户上传、下载权限和自动解析属于后续阶段；当前只要求预设选择页展示作者、来源和许可信息，运行包版本在房间创建时冻结。

### 2.2.1 当前资源归档清单

开发前置资料已集中到后端目录，当前只完成归档和完整性记录，尚未把原文编译成运行时规则或 ModulePack：

```text
trpg-backend/modules/presets/
  追书人/source/追书人.pdf
  银之锁/source/银之锁.docx
  林隙的罪恶/source/林隙的罪恶-Butterrr.doc
  坨子岛/source/模组正文.pdf

trpg-backend/rulesets/coc7/
  source/克苏鲁的呼唤 守秘人规则书 40周年纪念版.pdf
```

每个模组和规则集目录都有 `manifest.json`、SHA-256 和当前状态；模组的 `catalog.json` 只包含预设选择页需要的公开介绍。原始 PDF/DOC/DOCX 仅作为离线参考，不能直接作为 Kernel 状态源或生产 Prompt 的整本输入。

## 2.3 阶段依赖总表

| 阶段 | 先交付什么 | 依赖 | 阶段结束后能做什么 |
| --- | --- | --- | --- |
| Phase 0 | SDK 门禁、DTO、空骨架、人工预设安装 | 无 | 无模型也能创建房间、生成 Actor、提交 `wait` 并恢复 |
| Phase 1A | 权威状态、命令事务、检定、单人时间 | Phase 0 | 不依赖模型跑通一条可靠规则路径 |
| Phase 1B/1C | 意图理解、叙事、追书人完整内容 | Phase 1A | 单人模组可用真实模型完整游玩 |
| Phase 2 | 银之锁的物品、机关和条件结局 | Phase 1C | 单人复杂解谜可恢复、可验证 |
| Phase 3 | 林隙的罪恶多人投影、并发和战斗 | Phase 2 | 1-3 人可分队、协作和重连 |
| Phase 4 | 坨子岛长时间线和沙盒调度 | Phase 3 | 1-4 人长篇多人模组达到发布门禁 |
| Phase 5 | 自动解析、审核工作台和扩展规则 | Phase 4 | 从上传文档半自动生成待审核运行包 |

阶段不能跳过退出条件；但阶段内部可以并行处理前端样式、测试数据和文档，只要不改变权威契约。

## 3. Phase 0：技术门禁与最小契约

目标：先证明选型可用，再建立不会被后续推翻的最小边界。

开工前置资料已准备完成：四个预设原文、四个预设目录数据和 CoC7 规则书均已归档。Phase 0 仍需先建立加载器、Schema 和版本校验，不能因为文件已存在就视为运行包已经完成。

### P0-1 Agents SDK 模型兼容门禁

#### 开工执行输入（当前基线）

- 首个候选使用仓库现有 DeepSeek-compatible 配置：`HOST_MODEL_PROVIDER=deepseek`、
  `DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL` 和 `DEEPSEEK_API_KEY`，值从本地
  `trpg-backend/.env` 读取；密钥不得写入 Git、日志或验收记录。
- 当前配置只算“已准备”，不算门禁通过。必须实际运行一次 P0-1 smoke，确认地址、模型、
  Chat Completions JSON 输出、工具调用、超时/取消和错误分类；失败时不得用 fake provider
  冒充生产通过。
- 主持意图解释与叙事是低延迟结构化调用，默认关闭 provider thinking/reasoning；不得记录或展示思维链，只保留最终结构化结果和脱敏用量。
- 运行记录只保留 provider、模型名、SDK 版本、schema 版本、时间和脱敏结果。若更换模型或
  `base_url`，需重新执行门禁。

- 添加 `openai-agents` 依赖，不改业务入口。
- 定义一个 Pydantic 输出模型和一个只读函数工具。
- 支持配置 `base_url/api_key/model_name/api_style`，验证 OpenAI 与当前生产候选的 OpenAI-compatible 配置。
- 记录结构化输出、工具调用、超时取消、错误分类和 tracing 是否可用。
- OpenAI-compatible 接口不能保证能力等价；不兼容 OpenAI 协议的接口必须实现薄 model adapter，不自研 Agent loop。
- 增加仅供自动化测试使用的 `ScriptedModel`，不作为生产备用主持。
- 生产模型未通过启动检查时禁止开始新游戏；进行中的模型调用失败时持久化暂停 Turn，不推进时间、不掷骰、不写领域事件。

退出：至少一个生产候选 provider 全部通过；配置的地址和模型有自动启动检查；`ScriptedModel` 只能被测试配置加载。

### P0-2 冻结第一批 DTO

- 在 `dto/gm.py` 定义 Phase 0 实际使用的判别联合：
  - `CommandEnvelope`：`MoveActor`、`InspectTarget`、`TalkToNpc`、`WaitUntil`；
  - `DomainEventEnvelope`：移动、事实发现、时间推进；
  - `IntentResult`、`CommandResult`、`PlayerProjection`、`NarrationDraft`；
  - `TurnState`：收集、理解、校验、等待澄清、结算、叙事、完成。
- 所有 DTO 使用 `schema_version=1` 并拒绝未知字段。

退出：Schema 可导出到现有 SDK；非法 action、target 和额外字段会被拒绝。

### P0-3 制作最小《追书人》运行包

- 只录入第一条纵向路径需要的地点、对象、事实、线索、NPC、场景、时间和结局。
- 每个字段保留 `source_refs`，但运行时不加载整份 PDF。
- 添加引用完整性、ID 唯一性和玩家文本不含 keeper 字段的检查。

退出：运行包可以被纯 Python 加载并通过静态检查，不需要模型参与。

#### 素材边界

四个预设目录已提交到 `trpg-backend/modules/presets/`，每个目录包含 `catalog.json`、
`manifest.json`、README 和本地归档的原始文件。规则书只保留在本机
`trpg-backend/rulesets/coc7/source/`，由 `.gitignore` 排除，不作为仓库输入提交。
Phase 0 只读取已制作的 ModulePack；PDF/DOCX 自动解析不属于开工前置条件。

### P0-4 搭建新运行时空骨架

- 新建独立的 `gm` DTO、服务和测试入口，不复用旧 ActionPlan、Rule、Goal、Ending 的运行时编排。
- 增加 `GET /health`、创建新游戏会话、读取当前玩家投影和提交 Turn 的最小 API；旧房间 API 暂不删除，但不接入新运行时。
- 增加仅测试使用的 `ScriptedModel` 和 `Wait` 命令，使 CI 可以在无外部模型、无真实模组时验证整条链路；生产配置不得加载脚本模型或假主持。
- 所有未实现能力返回机器可识别的 `not_implemented`，前端显示可恢复错误，不写假事件。

退出：新进程可启动；创建房间、生成 Actor、提交 `wait`、重连读取投影和重复请求均有测试；没有任何代码能直接从玩家文本写权威状态。

### P0-5 内容存储与预设安装

- 增加原始文件元数据、ModulePack manifest、版本、哈希、来源和可见性记录；原文件内容放私有存储，不进入玩家投影。
- 先支持仓库内人工制作的《追书人》ModulePack 安装，不实现 PDF/DOCX 自动抽取。
- 创建房间时只保存 `module_id/module_version/content_hash`，运行中禁止无提示切换版本。

退出：预设可安装、列出、创建房间并按固定版本读取；版本哈希不匹配或权限不足会被拒绝；客户端接口不会返回完整 ModulePack。

## 4. Phase 1A：纯 Kernel 垂直切片

目标：不用大模型也能完整执行一条可靠游戏路径。

### P1A-1 最小权威数据

- 新建 `game_sessions`、运行时 Actor、事实/可见性、`turn_runs`、`pending_decisions`、`game_events`、`command_receipts` 和 `outbox_messages`。
- 新运行时继续使用现有 Room、Player 和 Character 表结构，不复制账号体系；只为新创建的房间生成 Actor。
- 创建房间运行实例时冻结 module/ruleset 版本，并从 Character 生成 Actor 快照。

前置条件已完成：本机旧房间为空，账号与用户角色卡库仍在。退出：新表迁移可在该干净基线上执行，数据库约束能阻止重复 receipt 和错误可见性引用。

### P1A-2 命令事务

- 实现统一命令入口：锁 session、校验 revision/receipt、执行命令、写状态/事件/回执/Outbox、递增 revision。
- 先实现移动、调查、交谈和等待。
- 为每条命令提供重复提交、过期 revision 和事务失败测试。

退出：同一 `command_id` 重放只返回原回执；一次命令可以写多个同 revision 事件。

### P1A-3 检定与暂停

- 增加 `StartCheck`、`check_runs` 和玩家点击投骰入口；首次请求由服务端生成骰点，重复请求按 `check_id` 返回原结果。
- 分开 `AwaitingRoll` 与骰后选择；第一批只实现当前《追书人》路径实际需要的选项。
- 拒绝客户端提交骰点值。

退出：重试和重启不会重掷；拒绝或待选择总有可发布 `CommandResult`。

### P1A-4 时间与 Scheduler

- 实现单人 `world_time`、行动耗时、`WaitUntil` 和定时事件分段推进。
- Scheduler 只生成系统命令，使用同一事务入口。
- 等待遇到可感知事件或玩家选择立即停止。

退出：可以从白天等待到夜晚；中途事件只触发一次并正确打断。

## 5. Phase 1B：接入 AI 主持

目标：模型理解和表达可以失败，但游戏状态仍可靠。

### P1B-1 Context Builder

- 从 Actor、PlayerProjection、当前地点和运行索引构造不可变 `ContextSnapshot`。
- Intent 只获得公开对象、行动候选和必要环境约束。
- 隐藏触发、未发现线索和 NPC 未披露知识只留在 Validator/Kernel。

退出：快照黄金测试覆盖守墓人、墓园环境和未触发线索；玩家快照中秘密为零。

### P1B-2 Intent Interpreter

- 使用 Agents SDK 单 Agent + Pydantic `IntentResult`。
- 首批只注册确实需要的只读工具。
- 实现目标唯一性、当前焦点、实质歧义和 proposal revision 校验。
- “过个侦察”必须询问目标；“观察守墓人的口袋”直接生成唯一提案。

退出：脚本模型测试稳定；真实模型 smoke 同时断言语义结果，而不只断言 Schema。

### P1B-3 Narrator 与 NPC 对话

- Kernel 先计算本次允许披露的 NPC 事实。
- Narrator 只收到已提交事件、受众可见事实和已批准披露集合。
- 确定性 Narration Guard 校验事件、事实、数值和受众；只有 Kernel 已提交成功、仅 Narrator 表达失败时，才用 `CommandResult.narration_facts` 展示确定性结果。

退出：诱导模型索要模组秘密时仍无泄漏；叙事失败不回滚 Kernel。

### P1B-4 现有 API、SDK 与前端接入

- 新运行时沿用现有房间凭证、聊天 WebSocket 和 Turn REST 入口。
- 前端支持自由文本、澄清选项、掷骰确认、骰后选择、重连恢复和错误提示。
- 不在本阶段重做房间、建卡、角色页或语音系统。

退出：浏览器可以完成第一条《追书人》路径并在任一等待点刷新恢复。

## 6. Phase 1C：《追书人》完整门禁

当前实现进度：`trpg-backend/modules/presets/追书人/runtime.json` 已建立结构化运行包，包含场景、技能、公开线索、检查点、夜间中断、破窗追逐、食尸鬼遭遇和结局切面；`tests/test_gm_paper_chase_playthrough.py` 已覆盖八类脚本门禁。真实 provider smoke、真实模型完整局、本地浏览器从建房到合法结局以及 GitHub PostgreSQL 16 migration 均已留存通过证据；Phase 1C 已合并到 `AI-KP`。

- 补齐该模组所需的调查路线、NPC 关系、关键线索恢复和多结局。
- 只实现该模组需要的 CoC7 战斗、步行追逐、HP、SAN 和疯狂子集。
- 增加文稿 13.1 的 scripted playthrough、故障注入和真实模型完整局。

退出：13.1 全部通过，才进入第二个预设；单条演示路径不算完成。

## 7. Phase 2：《银之锁》能力增量

- 只在此阶段新增世界对象、锁/钥匙、容器、距离限制和组合交互。
- 增加有限资源、动态物品定义和物品生成白名单。
- 增加谜题依赖静态检查及消耗品恢复测试。

退出：文稿 13.2 全部通过；不提前建设通用制作或魔法系统。

## 8. Phase 3：《林隙的罪恶》多人核心

- 增加逐 Actor 位置、库存、私密事实和受众投影。
- 实现 `free_play`、`party_window`、`initiative`。
- 实现 `action_instances`、资源预留、最早时间边界和固定锁顺序。
- 增加声音传播、NPC 最小行为状态机、陷阱、枪械、重伤和逐玩家结局。
- 增加 1 人与 3 人 scripted/real-model playthrough。

退出：文稿 13.3 全部通过；任一玩家断线不阻塞无关玩家。

## 9. Phase 4：《坨子岛》沙盒增量

- 增加多日 timeline、clock、天气、阵营、监视、追捕、抓捕和营救。
- 增加分队、合流、失联、部分被捕和逐玩家结局。
- 运行可达性检查、多路径完整局和 100 Turn 模型稳定性评测。

退出：文稿 13.4 全部通过，才能声明支持长篇多人沙盒。

## 10. Phase 5：导入工具，最后再做

- 四份人工运行包稳定后，才实现 PDF/DOCX 自动抽取、Parser/Review Agent 和发布工作台；P0-5 的文件存储、版本冻结和人工运行包安装不属于此阶段。
- 私人模组允许由 Parser Agent、Review Agent 和确定性检查自动生成 ModulePack，不强制上传者人工阅读秘密内容；解析警告应展示风险但不剧透正文。
- 导入产物必须经过来源、Schema、引用、隐私和可达性自动检查；只有发布为平台公共预设时才进入可选的人工发布审核。
- 不自动发布未授权原文或衍生运行包。

## 11. 明确不做

- 不兼容旧 ActionPlan/Rule/Goal/Ending。
- 不自研 Agent loop，不同时引入 LangGraph。
- 不创建常驻 NPC Agent。
- 不在前四个预设通过前建设插件市场、向量数据库或通用规则平台。
- 不用聊天历史、模型 session 或 LangGraph checkpoint 充当权威游戏状态。

## 12. Phase 0 实施契约

本节是 Phase 0 的可执行补充。未列入本节的最终能力不属于首批实现；实现以本节和前文全局完成定义为准。

### 12.1 资源关系与首批 API

首批资源关系固定为：`Room -> GameSession -> Actor`。一个 Room 首版只允许一个活动 GameSession；Session 创建后冻结 `module_id/module_version/content_hash/ruleset_version/ruleset_profile`。

首批接口为 `GET /health`、`POST /api/gm/sessions`、`GET /api/gm/sessions/{session_id}/projection`、`POST /api/gm/sessions/{session_id}/turns`。Turn 请求必须包含 `client_request_id`、`actor_id`、`expected_revision` 和 `input`；`player_id` 从认证会话取得，客户端不能传入 `room_id`、受众或骰点。相同请求 ID 重放返回原结果，不同请求摘要返回 `duplicate_request`。

首批只承诺 `WaitUntil` 的 Stub 路径；`MoveActor`、`InspectTarget`、`TalkToNpc` 先完成 DTO 和拒绝未知字段测试，再按阶段实现。

### 12.2 DTO 最小字段

```text
CommandEnvelope: schema_version, command_id, room_id, session_id, turn_id,
  actor_id, expected_revision, command_type, payload
CommandResult: schema_version, command_id, status, committed_revision,
  events, pending_decisions, narration_facts, error
PlayerProjection: schema_version, session_id, player_id, revision,
  world_time, actors, visible_facts, pending_decisions
TurnState: collecting | understanding | validating | awaiting_clarification |
  awaiting_roll | resolving | narrating | completed | failed
```

所有 ID 使用 UUID 字符串，时间使用 UTC ISO 8601；Pydantic 模型拒绝未知字段。空输入、不属于当前 Actor 的 target 和客户端骰点均在边界层拒绝。

### 12.3 数据库、迁移与 CI

- 新运行时只支持 PostgreSQL 15+；CI 固定使用 `postgres:16`，本地使用同镜像的
  `trpg-backend/docker-compose.postgres.yml`；`app.db` 仅供旧运行时保留。
- 本地启动：`docker compose -f trpg-backend/docker-compose.postgres.yml up -d`，然后设置
  `DATABASE_URL=postgresql+asyncpg://trpg:trpg@127.0.0.1:5432/trpg_test` 执行迁移和测试。
- 使用仓库唯一的 migration 工具；本地、CI、测试环境使用同一 PostgreSQL 镜像。
- CI 从空库执行迁移，再运行 DTO、Kernel、权限和恢复测试。
- Phase 0 最小表集为 `game_sessions`、`actors`、`turn_runs`、`turn_inputs`、`game_events`、`command_receipts`、`outbox_messages`。
- 必须测试并发提交、重复 `command_id`、过期 revision、提交后进程中断和 Outbox 重投。

### 12.4 Provider 验证

提交门禁使用仅测试可加载的 `ScriptedModel`；真实模型 smoke 为部署前或定时门禁。生产统一配置 `GM_MODEL_BASE_URL`、`GM_MODEL_API_KEY`、`GM_MODEL_NAME`、`GM_MODEL_API_STYLE`。缺少配置、地址不可达或能力检查失败时，服务健康状态标记模型不可用并禁止开始新游戏。

Smoke 使用同一份 Pydantic 输出和一个只读工具，断言结构化输出、工具调用、超时、取消、错误分类和 tracing；记录 provider、model、SDK 版本、schema 版本和测试时间，不记录密钥或思维链。OpenAI Responses 兼容接口走原生路径，OpenAI Chat Completions 兼容接口走兼容路径，其他协议需要薄 adapter。不能通过门禁的任意地址或模型不得进入生产。

Intent Interpreter 或 Adjudication Advisor 调用失败时返回 `gm_unavailable` 并把当前 Turn 持久化为可恢复暂停，不执行 Kernel。Narrator 失败时，只有本回合已经存在 committed receipt，才允许把确定性结算事实展示给玩家；不得用预设对白或假主持继续游戏。

### 12.5 第一条自动化脚本

Phase 0 golden playthrough 固定为：安装《追书人》Phase 0 ModulePack，创建单人 Session，提交“等待到夜晚”，Stub 生成 `WaitUntil`，Kernel 写入时间推进事件和 receipt；重复提交同一 `client_request_id` 不新增事件；模拟重启后仍读取相同 revision 和投影；投影不含 `keeper/private(other)` 字段。每一步检查 DTO、revision、事件数量和可见字段，不只检查 HTTP 状态码。

### 12.6 首批错误码

`not_implemented`、`revision_conflict`、`duplicate_request`、`visibility_denied`、`invalid_command`、`gm_unavailable`、`provider_timeout`、`storage_unavailable`。错误响应统一包含 `schema_version`、`code`、`message`、`retryable`；权限错误不得泄漏对象是否存在。

### 12.7 未授权模组隔离

四个预设的开发安装包可以按项目实际可用范围放在本地或受限仓库，并供测试房间运行。公开仓库是否包含原文或完整改编包，另按每个模组的许可决定；不允许把未确认可公开分发的内容放入公共下载渠道。每个 manifest 至少记录作者、来源、许可、内容哈希和审核状态；缺少作者或来源信息的包不能进入预设选择页。

### 12.8 开放行动、检定与技能规则

玩家自然语言不需要穷举。Intent Interpreter 只映射有限动作原语、当前对象 ID、行动方式和不确定字段；模组未规定的合理行动由 Adjudication Advisor 根据 CoC7 技能规则、环境压力和失败后果提出裁决，Kernel 最终校验并提交。

角色有能力、没有明显压力且失败没有有意义后果时直接成功；结果不确定、存在风险且成功失败都会改变局面时才建立 `CheckRun`。AI 可以建议是否检定、使用什么技能和难度，但骰点只能由服务端生成，结果只能由 Kernel 应用。

CoC7 技能不能只保存名称和基础值。每个已实现技能至少结构化记录用途、适用时机、无需检定时机、典型目标、不适用目标、难度指导、失败后果、关联技能、来源引用和实现状态。先覆盖《追书人》实际使用的技能，再随四个预设增量补齐，不在 Phase 0 一次录完整本规则书。

### 12.9 上下文切片与叙事一致性

四个预设的 ModulePack 预先按地点、场景、Actor、对象、事实、目标图、时间线和来源片段切分。Context Builder 按当前 `location_id/situation_id/action_type/audience/revision` 选择切片；Phase 0-4 不依赖向量 RAG。未来自动导入模组生成相同结构，因此运行时接口不分叉。

Narrator 只表达已提交事件和当前受众可见事实。Actor 死亡、失能、位置和物品均从权威投影读取；已死亡 NPC 不得重新进入存活候选。Narrator 若声称其行动、对话或复活，Narration Guard 必须拒绝；仅在本回合已经提交成功时，才使用确定性结算事实替代文学叙事。

/**
 * 本文件由 `npm run codegen` 从后端 pydantic 模型自动生成，请勿手改。
 *
 * 源头：trpg-backend/app/dto/ 下由 export_schema.py 登记的 DTO（含 host_speech.py）
 * 重新生成：
 *   1. cd trpg-backend && uv run python scripts/export_schema.py
 *   2. cd trpg-sdk && npm run codegen
 * 生成后把这个文件的改动一并提交——CI 会重新跑一遍上面两步，用 git diff
 * 校验有没有人改了后端 DTO 却忘记重新生成（issue #75 决策 3）。
 */

/**
 * 保留前端提交形状；新 GM Agent 接入前由服务端明确拒绝。
 */
export interface ActionSubmitPayload {
  clientActionId: string;
  utterance: string;
}

/**
 * 调查员年龄的合法区间（issue #96）。
 *
 * COC7 的年龄档从 15-19 起、到 80-89 止，所以合法区间是 [15, 89]。此前前端
 * 的输入框写死成 [10, 100]，两头都不符合规则。
 *
 * 注意本期只做区间约束，**不做年龄修正**（15-19 岁扣 STR/SIZ/EDU 各若干、
 * 20-39 岁一次教育增强检定、40 岁起每十年 MOV -1 等）——那是一整套生成期
 * 规则，要单独做。
 */
export interface AgeRangeSpec {
  minValue: number;
  maxValue: number;
}

/**
 * 点数购买法的约束（issue #96）。
 *
 * 这些数字此前只存在于前端代码里、后端既不校验也不暴露，导致 ①任何 SDK
 * 使用者都能提交 UI 永远不允许的角色卡 ②重写前端时必须把规则再实现一遍。
 * 放进 ruleset 是为了「一份定义、两方消费」：后端拿它裁决，客户端拿它渲染
 * 「还剩多少点」「这项最多加到多少」。
 *
 * 只约束 `point_buy=True` 的属性；幸运不在其列。
 */
export interface AttributePointBuyRules {
  budget: number;
  minValue: number;
  maxValue: number;
  defaultValue: number;
}

/**
 * 一项基础属性：键名、显示名、COC7 生成公式。
 *
 * `point_buy` 表示这一项是否参与点数购买法的分配。COC7 里幸运只能掷
 * （`3d6*5`）、不能用属性点买，所以它是 `False`——客户端据此决定哪些属性
 * 渲染成可加点、哪些只读展示，不需要自己维护一份"哪 8 项能加点"的名单
 * （issue #96：这份名单此前在前端硬编码了三处，加幸运时漏改一处导致
 * 角色卡看不到幸运值）。
 */
export interface AttributeSpec {
  key: string;
  label: string;
  generation: string;
  pointBuy?: boolean;
}

/**
 * 注册 / 登录成功后的返回：登录凭证 + 用户信息。
 */
export interface AuthResult {
  token: string;
  userId: string;
  nickname: string;
}

/**
 * `compute_preview` 的响应结构：衍生值 + 两个技能点预算 + 全部技能的
 * base/cap/当前值 + 校验报告。
 */
export interface CharacterComputeResult {
  derivedStats: {
    [k: string]: number | string;
  };
  occupationSkillPoints: SkillPointsBudgetView;
  interestSkillPoints: SkillPointsBudgetView;
  skillView: SkillComputeView[];
  resolvedOccupationChoiceSkillIds?: string[];
  validation: ValidationIssueView[];
}

/**
 * POST /api/v1/rooms/{roomId}/characters 返回
 */
export interface CharacterDraftResult {
  characterId: string;
  status: string;
}

/**
 * POST /api/v1/systems/{systemId}/character/preview 请求体。
 */
export interface CharacterPreviewRequest {
  attributes: {
    [k: string]: number;
  };
  occupationId?: number | null;
  skills?: {
    [k: string]: number;
  };
  occupationChoiceSkillIds?: string[] | null;
  generationMethod?: "pointbuy" | "roll";
}

/**
 * GET /api/v1/rooms/{roomId}/characters/{characterId} 返回（issue #96）。
 *
 * 补这个端点是为了让**后端成为角色卡的唯一事实来源**。此前只有
 * 创建/保存/完成/掷属性四个写操作、没有任何读接口，前端因此只能把角色卡
 * 存进 localStorage 当权威源——而那份副本的结构会随后端 schema 演进而过期
 * （PR #88 加幸运后，旧的 8 键角色卡就再也编辑不了了）。
 *
 * `generation_method` 一并返回：客户端要据此知道这张卡该按点数购买法还是
 * 掷骰法来渲染与校验。
 */
export interface CharacterRead {
  id: string;
  status: string;
  basedOnTemplateId?: string | null;
  generationMethod: string;
  name?: string | null;
  age?: number | null;
  gender?: string | null;
  residence?: string;
  birthplace?: string;
  attributes?: {
    [k: string]: number;
  };
  derivedStats?: {
    [k: string]: number | string;
  };
  skills?: {
    [k: string]: number;
  };
  occupationChoiceSkillIds?: string[] | null;
  equipment?: string[];
  occupation?: string | null;
  background?: string;
  notes?: string;
}

/**
 * POST /api/v1/me/character-templates 请求体。
 *
 * `data.generation_method` 由服务端决定，这里传什么都会被覆盖成点数购买法：
 * 「属性是掷出来的」是一条**服务端背书**，只有服务端自己掷的那一次才能给出
 * （见 `POST /me/character-templates/{id}/roll-attributes`）。让客户端自己声明
 * 的话，它只要写上 roll 就能跳过 `complete` 的点数预算校验，8 项全 90 也能过。
 */
export interface CharacterTemplateCreateBody {
  name: string;
  systemId: string;
  data?: {
    [k: string]: unknown;
  };
}

/**
 * `我的常用角色卡` 列表/详情返回项。
 */
export interface CharacterTemplateRead {
  templateId: string;
  name: string;
  systemId: string;
  data: {
    [k: string]: unknown;
  };
  hasPortrait?: boolean;
  portraitVersion?: string | null;
  createdAt: string;
  updatedAt: string;
}

/**
 * PATCH /api/v1/me/character-templates/{templateId} 请求体（#337）。
 *
 * 卡库现在也是建卡的宿主，建卡向导的每一次保存都落到这里，所以要能只改名、
 * 只改数据、或两个都改——两个字段都可选，`None` 表示这一次不动它。
 *
 * `data` 命中时是**整体覆盖**而不是合并：合并语义下前端删掉一项技能永远删不掉。
 *
 * `data.generation_method` 同样不由客户端决定：只有「这次 PATCH 没有改动属性」
 * 时服务端背书的 roll 才会保留，其余一律退回点数购买法。这跟房间版
 * `update_character` 对 `roll` 的处理是同一道闸。
 */
export interface CharacterTemplateUpdateBody {
  name?: string | null;
  data?: {
    [k: string]: unknown;
  } | null;
}

/**
 * PATCH /api/v1/rooms/{roomId}/characters/{characterId} 请求体
 */
export interface CharacterUpdateBody {
  name: string;
  age?: number | null;
  gender?: string | null;
  residence?: string;
  birthplace?: string;
  attributes: {
    [k: string]: number;
  };
  derivedStats: {
    [k: string]: number;
  };
  skills: {
    [k: string]: number;
  };
  occupationChoiceSkillIds?: string[] | null;
  equipment?: EquipmentItem[];
  occupation?: string | null;
  background?: string;
  notes?: string;
}

export interface ChatMessagePayload {
  messageId: string;
  playerId: string;
  nickname: string;
  text: string;
  sentAt: string;
  clientMessageId: string;
}

/**
 * 讨论区一条消息。`sent_at` 用 UtcDatetime（对外时间字段的统一约定，
 * 见 app/dto/common.py）。
 */
export interface ChatMessageRead {
  messageId: string;
  playerId: string;
  nickname: string;
  text: string;
  sentAt: string;
  clientMessageId: string;
}

/**
 * 玩家讨论区消息，不进入任何模型上下文。
 */
export interface ChatSendPayload {
  text: string;
  clientMessageId: string;
}

/**
 * 对玩家公开的检定状态，不包含 keeper 过程数据。
 */
export interface CheckRead {
  checkId: string;
  skillId: string;
  difficulty: "regular" | "hard" | "extreme";
  status: "awaiting_roll" | "resolved";
  roll?: number | null;
  targetValue: number;
  success?: boolean | null;
}

/**
 * 客户端或 Intent Interpreter 提交的幂等命令信封。
 */
export interface CommandEnvelope {
  schemaVersion?: 1;
  clientRequestId: string;
  expectedRevision: number;
  actorId: string;
  command: MoveActor | InspectTarget | TalkToNpc | WaitUntil | StartCheck | RollCheck;
}

/**
 * 一次命令提交的确定性结果和最新玩家投影。
 */
export interface CommandResult {
  schemaVersion?: 1;
  clientRequestId: string;
  revision: number;
  events: DomainEventEnvelope[];
  projection: PlayerProjection;
  pendingDecisions?: PendingDecision[];
  check?: CheckRead | null;
}

/**
 * Kernel 提交给事件日志和 Narrator 的领域事件。
 */
export interface DomainEventEnvelope {
  schemaVersion?: 1;
  eventId: string;
  eventType: string;
  actorId: string;
  visibility?: "public" | "private" | "hidden";
  payload: {
    [k: string]: unknown;
  };
}

export interface EquipmentItem {
  name: string;
}

/**
 * 统一错误码枚举。
 *
 * 用 StrEnum（Python 3.11+）而不是普通字符串常量或 int 枚举，好处是：
 * - 序列化成 JSON 时直接是字符串值（比如 "NOT_FOUND"），前端/SDK 拿到的就是可读的码；
 * - 类型检查器（ty/mypy）能校验到哪些地方在用错误码，重命名/新增时不会漏改；
 * - 每个成员名本身就是 UPPER_SNAKE_CASE，跟成员值保持一致，一眼能看出对应关系。
 *
 * 新增错误码时，在这里加一行即可；用哪个 HTTP 状态码由抛出方（业务代码里的
 * AppException(...) 调用）决定，这个枚举本身不绑定状态码。
 */
export type ErrorCode =
  | "VALIDATION_ERROR"
  | "BAD_REQUEST"
  | "UNAUTHORIZED"
  | "FORBIDDEN"
  | "NOT_FOUND"
  | "CONFLICT"
  | "CHARACTER_TEMPLATE_DUPLICATE"
  | "INTERNAL_ERROR"
  | "ROOM_NOT_FOUND"
  | "ROOM_FULL"
  | "MODULE_VALIDATION_FAILED"
  | "NOT_YOUR_TURN"
  | "ACTION_IN_PROGRESS"
  | "CHARACTER_INCOMPLETE"
  | "MODULE_NOT_SELECTED"
  | "MODULE_PLAYER_COUNT_MISMATCH"
  | "RECONNECT_TOKEN_EXPIRED"
  | "RATE_LIMITED"
  | "NOT_IMPLEMENTED"
  | "CHARACTER_INVALID"
  | "RULESET_NOT_CONFIGURED"
  | "PORTRAIT_GENERATION_DISABLED"
  | "PORTRAIT_GENERATION_IN_PROGRESS"
  | "PORTRAIT_CONTENT_REJECTED"
  | "PORTRAIT_GENERATION_FAILED"
  | "PORTRAIT_GENERATION_TIMEOUT"
  | "HOST_SPEECH_UNAVAILABLE"
  | "HOST_SPEECH_FAILED"
  | "HOST_SPEECH_TIMEOUT"
  | "HOST_MODEL_UNAVAILABLE"
  | "REVISION_CONFLICT"
  | "ITEM_VERSION_CONFLICT"
  | "ITEM_ALREADY_TAKEN"
  | "ENDING_UNAVAILABLE"
  | "ENDING_DRAFT_STALE"
  | "TURN_NOT_FOUND"
  | "TURN_RESUME_UNAVAILABLE";

/**
 * 错误信息的具体内容，只在 success=false 时出现在 error 字段里。
 *
 * `details` 是 issue #84 S2 新增的可选字段：装结构化的校验报告（比如建卡
 * 校验失败时的一条条 {code, field, message}），大多数错误不需要它，默认
 * None，不影响原有只有 code/message 的错误响应形状。
 */
export interface ErrorDetail {
  code: ErrorCode;
  message: string;
  details?:
    | {
        [k: string]: string;
      }[]
    | null;
}

export interface ErrorPayload {
  code: string;
  message: string;
  correlationId?: string | null;
}

/**
 * 游戏大类。
 */
export interface GameRead {
  id: string;
  name: string;
  description?: string | null;
  tags?: string[];
}

/**
 * 请求从建卡阶段进入基础游戏页面。
 */
export interface GameStartPayload {}

/**
 * 大类下的规则系统。
 */
export interface GameSystemRead {
  id: string;
  gameId: string;
  worldRef: string;
  name: string;
  version?: string | null;
  worldName?: string | null;
  worldDescription?: string | null;
  gameDescription?: string | null;
}

/**
 * 请求调查当前可见的地点或对象。
 */
export interface InspectTarget {
  kind: "inspect_target";
  targetId: string;
}

/**
 * POST /api/v1/rooms/{roomCode}/join 请求体
 */
export interface JoinRoomBody {
  nickname?: string | null;
}

/**
 * POST /api/v1/auth/login 请求体
 */
export interface LoginBody {
  account: string;
  password: string;
}

/**
 * GET /PATCH /api/v1/auth/me 返回
 */
export interface MeRead {
  userId: string;
  account: string;
  nickname: string;
}

/**
 * GET /api/v1/modules/{moduleId} 返回——增加故事页展示信息。
 */
export interface ModuleDetailRead {
  id: string;
  gameSystemId: string;
  gameSystemName?: string | null;
  title: string;
  nameEn?: string | null;
  version: string;
  status: string;
  authors: string[];
  playersMin: number;
  playersMax: number;
  difficulty: number;
  estimatedDuration?: string | null;
  synopsis?: string | null;
  storyLabel?: string | null;
  subtitle?: string | null;
  storyPages: ModuleStoryPage[];
}

/**
 * POST /api/v1/modules/import 与 GET /api/v1/modules/import/{jobId} 返回。
 *
 * 不用 `from_attributes` 直接从 ORM 对象转换——ORM 主键列叫 `id`，这里
 * 对外字段叫 `job_id`（避免跟其它 DTO 的 `xxxId` 命名约定不一致），两者
 * 对不上，构造时由 service 层显式传关键字参数更直接。
 */
export interface ModuleImportJobRead {
  jobId: string;
  status: string;
  sourceFilename?: string | null;
  resultScenarioId?: string | null;
  errorMessage?: string | null;
  createdAt: string;
  updatedAt: string;
}

/**
 * POST /api/v1/modules/import 请求体。
 *
 * 真实实现（#57）会接收模组原始文档做 LLM 解析，本期这个接口固定返回
 * NOT_IMPLEMENTED，请求体只占位描述"以后大概会传什么"，不做内容校验。
 */
export interface ModuleImportRequestBody {
  sourceFilename: string;
}

/**
 * 模组信息（对应内容库 `Scenario` 表，`from_attributes=True` 支持直接从
 * ORM 对象构造）。
 */
export interface ModuleRead {
  id: string;
  gameSystemId: string;
  gameSystemName?: string | null;
  title: string;
  nameEn?: string | null;
  version: string;
  status: string;
  authors: string[];
  playersMin: number;
  playersMax: number;
  difficulty: number;
  estimatedDuration?: string | null;
  synopsis?: string | null;
}

/**
 * 玩家开局前可见的一页故事介绍。
 */
export interface ModuleStoryPage {
  title: string;
  content: string;
}

/**
 * 请求把当前调查员移动到已知地点。
 */
export interface MoveActor {
  kind: "move_actor";
  targetId: string;
}

/**
 * GET /api/v1/me/rooms 返回项
 */
export interface MyRoomSummary {
  roomId: string;
  roomCode: string;
  roomName: string;
  phase: string;
  moduleTitle?: string | null;
  playerCount: number;
  maxPlayers: number;
  updatedAt: string;
}

/**
 * Narrator 生成的候选文本，必须引用已提交事件。
 */
export interface NarrationDraft {
  text: string;
  /**
   * @minItems 1
   */
  evidenceEventIds: [string, ...string[]];
}

/**
 * 职业选择器里的展示分类。规则目录决定分类与顺序，前端只负责渲染。
 */
export interface OccupationCategorySpec {
  label: string;
  icon: string;
}

/**
 * 一个职业：信用评级区间、职业技能点公式、职业技能清单与展示元数据。
 *
 * 职业技能 = `skill_ids`（固定）+ `choice_slots`（自选，见 `SkillChoiceSlot`）。
 * 两者都吃职业技能点；其余技能吃兴趣点。
 */
export interface OccupationSpec {
  id: number;
  name: string;
  creditMin: number;
  creditMax: number;
  skillPointsFormula: string;
  skillIds: string[];
  choiceSlots?: SkillChoiceSlot[];
  description: string;
  icon?: string | null;
  categories?: string[];
}

/**
 * 玩家必须完成的 Kernel 决策点，例如投骰。
 */
export interface PendingDecision {
  decisionId: string;
  kind: "roll_check";
  checkId: string;
  options: string[];
}

/**
 * 只包含当前玩家可见的稳定投影，不允许出现 keeper 字段。
 */
export interface PlayerProjection {
  sessionId: string;
  actorId: string;
  revision: number;
  worldTime: string;
  locationId: string;
  visibleFacts: string[];
  pendingCommandId?: string | null;
}

/**
 * 设置玩家准备状态。
 */
export interface PlayerReadyPayload {
  ready: boolean;
}

export interface PortraitGenerationRequest {
  style?: "realistic";
  size?: "1024x1024";
}

/**
 * 可安全返回给玩家的生图任务快照。
 */
export interface PortraitGenerationTaskRead {
  generationId: string;
  status: "queued" | "generating" | "cancelling" | "completed" | "failed" | "cancelled";
  cancelRequested: boolean;
  failureCode?:
    ("content_rejected" | "timeout" | "provider_failed" | "materialization_failed" | "process_restarted") | null;
  portraitVersion?: string | null;
  promptSummary?: string | null;
  promptSource?: ("deepseek" | "deterministic" | "deterministic_fallback") | null;
  style: "realistic";
  size: "1024x1024";
  createdAt: string;
  startedAt?: string | null;
  finishedAt?: string | null;
  updatedAt: string;
}

/**
 * 可选的玩家身份信息；旧客户端不传时仍使用确定性默认资料。
 */
export interface QuickGenerateRequest {
  name?: string | null;
  age?: number | null;
  gender?: string | null;
  residence?: string;
  birthplace?: string;
}

/**
 * 一键生成后返回的房间角色草稿和权威计算结果。
 */
export interface QuickGenerateResult {
  character: CharacterRead;
  occupationId: number;
  compute: CharacterComputeResult;
}

/**
 * POST /api/v1/auth/register 请求体
 */
export interface RegisterBody {
  account: string;
  password: string;
  nickname: string;
}

/**
 * POST /api/v1/rooms/{roomId}/characters/{characterId}/roll-attributes 返回。
 *
 * 服务端权威掷骰（COC7 标准法）：STR/CON/DEX/APP/POW = 3d6*5，
 * SIZ/INT/EDU = (2d6+6)*5；衍生值按标准公式算出 HP/MP/SAN，写回
 * `characters.attributes`/`derived_stats` 后原样返回给客户端展示。
 */
export interface RollAttributesResult {
  attributes: {
    [k: string]: number;
  };
  derivedStats: {
    [k: string]: number;
  };
}

/**
 * 请求结算已建立的检定；骰点始终由 Kernel 生成。
 */
export interface RollCheck {
  kind: "roll_check";
  checkId: string;
}

/**
 * GET /api/v1/rooms/{roomId}/conversation 返回项。
 *
 * 当前只承载讨论区消息；行动频道将在新 GM Agent 协议中重新定义。
 */
export interface RoomConversationEventRead {
  id: string;
  type: "chat.message";
  channel: "discussion";
  payload: {
    [k: string]: unknown;
  };
  createdAt: string;
}

/**
 * POST /api/v1/rooms 请求体
 */
export interface RoomCreate {
  nickname?: string | null;
  roomName: string;
  maxPlayers?: number;
}

/**
 * POST /api/v1/rooms 返回
 */
export interface RoomCreateResult {
  roomId: string;
  roomCode: string;
  reconnectToken: string;
  playerId: string;
  characterId?: string | null;
}

/**
 * 使用房间重连凭证绑定当前 WebSocket 身份。
 */
export interface RoomJoinPayload {
  reconnectToken: string;
  roomCode?: string | null;
  nickname?: string | null;
}

/**
 * 房间内玩家摘要。
 *
 * 注意 `player_id` 对应 ORM `Player` 的主键属性 `id`（名字不一样），所以不能直接
 * `model_validate(player_orm)`——调用方需要显式映射 `player_id=p.id`（见
 * service/room.py 的 _to_room_preview）。`from_attributes=True` 仍保留，方便
 * 其余名字一致的字段。camelCase 别名生成、populate_by_name 继承自 `CamelModel`——
 * pydantic 的 `model_config` 在子类里是合并而非整体覆盖父类配置，这里不需要
 * 重复声明（issue #77 审计发现 #1，原先这里重写了一份和父类一样的配置，是
 * #75 遗留的死代码）。
 */
export interface RoomPlayerRead {
  playerId: string;
  nickname: string;
  isHost: boolean;
  ready: boolean;
  hasCharacter: boolean;
  hasPortrait?: boolean;
  portraitVersion?: string | null;
}

/**
 * GET /api/v1/rooms/{roomCode} 返回
 */
export interface RoomPreview {
  roomId: string;
  roomCode: string;
  roomName: string;
  phase: string;
  storyStarted: boolean;
  moduleId?: string | null;
  moduleTitle?: string | null;
  playerCount: number;
  maxPlayers: number;
  players: RoomPlayerRead[];
}

export interface RoomStatePayload {
  roomId: string;
  phase: string;
  players: RoomPlayerRead[];
}

/**
 * 建卡所需的规则数据：属性/技能/职业目录（`GET /systems/{systemId}/ruleset`）。
 */
export interface RulesetRead {
  attributes: AttributeSpec[];
  attributePointBuy?: AttributePointBuyRules | null;
  ageRange?: AgeRangeSpec | null;
  skills: SkillSpec[];
  occupations: OccupationSpec[];
  occupationCategories?: OccupationCategorySpec[];
}

/**
 * POST /api/v1/rooms/{roomId}/module 请求体
 */
export interface SelectModuleBody {
  moduleId: string;
  attributeGenMethod?: string;
}

export interface SessionBoundPayload {
  roomId: string;
  playerId: string;
}

/**
 * 创建新 GM 会话时冻结的房间、模组和调查员信息。
 */
export interface SessionCreateBody {
  roomId: string;
  moduleId: string;
  actorId: string;
  displayName: string;
}

/**
 * 创建或重连后返回的玩家安全会话投影。
 */
export interface SessionRead {
  sessionId: string;
  moduleId: string;
  moduleVersion: string;
  projection: PlayerProjection;
}

/**
 * 职业技能里的一个「自选槽」：从 `candidate_skill_ids` 里选 `count` 项，
 * 选中的技能算**职业技能**（吃职业技能点），而不是兴趣技能（issue #114）。
 *
 * COC7 的职业技能不是一份固定清单，而是「固定技能 + N 个自选槽」，例如
 * 私家侦探是「技艺（摄影），乔装，法律，图书馆，**一项社交技能**（取悦、
 * 话术、恐吓、说服），心理学，侦查，**任意一项**其他个人或时代特长」。
 * 229 个职业里有 221 个（96.5%）至少带一个槽。
 *
 * 此前数据模型只有固定 `skill_ids`，装不下槽，于是"一项社交技能（四选一）"
 * 只能被压平成固定两项——这正是现有 30 个职业技能列表失真的原因（全部被
 * 规整成恰好 8 项，而规则书里实际是 0–15 项），并且会**误杀合法角色卡**：
 * 玩家把点数加在规则书认可、但被压平时丢掉的本职技能上，会被当成兴趣技能
 * 计费而触发 `INTEREST_POINTS_EXCEEDED`。
 *
 * `candidate_skill_ids` 为 `None` 表示**任意技能**（规则书里的"任意 N 项
 * 其他个人或时代特长"）；给出列表则表示限定候选集（如社交技能四选一）。
 */
export interface SkillChoiceSlot {
  count: number;
  candidateSkillIds?: string[] | null;
  label: string;
}

/**
 * 一项技能的计算结果：基础值/已分配点数/当前值/上限。
 */
export interface SkillComputeView {
  id: string;
  base: number;
  allocated: number;
  current: number;
  cap: number;
}

/**
 * 一个技能点池（职业/兴趣）的预算/已用/剩余。
 */
export interface SkillPointsBudgetView {
  budget: number;
  spent: number;
  remaining: number;
}

/**
 * 一项技能：基础值可以是固定数字，也可以是依赖属性的公式字符串
 * （比如闪避 `DEX/2`、母语 `EDU`）。
 */
export interface SkillSpec {
  id: string;
  name: string;
  nameEn?: string | null;
  base: number | string;
  category: string;
  relatedAttr?: string | null;
}

/**
 * 请求建立一次服务端检定；客户端只能提供目标技能和难度，不能提供骰点。
 */
export interface StartCheck {
  kind: "start_check";
  checkId: string;
  skillId: string;
  goal: string;
  difficulty?: "regular" | "hard" | "extreme";
}

/**
 * POST /api/v1/systems/{systemId}/character/quick-generate 返回（#337）。
 *
 * 不依赖房间的一键生成。跟房间版 `QuickGenerateResult` 的区别是它**不落库**：
 * 没有 `characterId`，也没有 `status`，只把生成结果交回客户端，由客户端决定
 * 存进哪张卡库卡（`PATCH /me/character-templates/{id}`）。
 *
 * `data` 与 `CharacterTemplateRead.data` 同形，可以原样 PATCH 回去，中间不用
 * 再拼一次字段。
 */
export interface SystemQuickGenerateResult {
  data: {
    [k: string]: unknown;
  };
  occupationId?: number | null;
  compute?: CharacterComputeResult | null;
}

/**
 * 请求与当前可见 NPC 交谈。
 */
export interface TalkToNpc {
  kind: "talk_to_npc";
  targetId: string;
  topic?: string;
}

/**
 * 回合生命周期的最小判别状态。
 */
export interface TurnState {
  value:
    | "collecting"
    | "understanding"
    | "validating"
    | "awaiting_clarification"
    | "awaiting_roll"
    | "resolving"
    | "narrating"
    | "completed"
    | "failed";
}

/**
 * PATCH /api/v1/auth/me 请求体
 */
export interface UpdateNicknameBody {
  nickname: string;
}

/**
 * 一条结构化校验失败信息，空列表代表这张卡合法。
 */
export interface ValidationIssueView {
  code: string;
  field: string;
  message: string;
}

/**
 * 请求把时间推进到模组允许的目标时间。
 */
export interface WaitUntil {
  kind: "wait_until";
  targetTime: string;
}

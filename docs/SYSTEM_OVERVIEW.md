# 系统与接口总览：Rule Engine

2026-10-27，按本批起始源码 `f1a3235` 核对。此页解释当前结构与阅读入口，
详细协议见[Stateless 合同](dev/stateless-evaluation.md)，当前支持范围见
[能力矩阵](capabilities.md)及[迁移进度](dev/stateless-migration-progress.md)。
跨模块权威是 sibling 文档库的 `docs/MODULE_CONTRACTS.md`，尤其 §2.3／§4／§6；
在平行 worktree 中按逻辑父目录定位，不从目标示例猜测现行 ABI。

## 1. 仓库定位与实际边界

本库是包含 Engine、SRD Data、Bridge、Demo 的 uv workspace。Engine 对显式输入
执行受支持 D&D 机械规则；SRD Data 提供类型化素材和加载边界。
当前 Stateless 路径不持有跨请求世界权威、不读 SM SQL、不提交 SM 世界事实。
但 Legacy combat loop 和 Bridge 确有进程内工作状态，不能说全仓库所有路径都无状态。

Stateless 转换仍是受限能力集。Legacy 和 Demo 中可运行的能力，不自动表示新的
Stateless 接口或 SM pin／采用白名单支持它；完整迁移与跨仓库 ABI 批准单独跟踪。
本库的 Bridge HTTP 和 Demo 也不代表 Agentic TRPG 已交付完整 Host／玩家 UI。

## 2. 包和主要规则模块

下表中 Engine 文件均相对 `packages/dnd5e-engine/src/dnd5e_engine/`；其他包写完整路径。
源码根入口另见 `packages/dnd5e-engine/src/dnd5e_engine/__init__.py` 的 `__all__`。
它固定原顶层导出；Stateless API 从下列明确模块导入，不能假定都由顶层导出。

| 模块／子系统 | 核心职责 | 文件／关键符号 | 主要协作对象 |
| --- | --- | --- | --- |
| Stateless 分发 | 重新验证固定请求、绑定，按操作进入已迁移切片 | `evaluation.py::evaluate` | 各 evaluator、结果／Delta／RNG 验证 |
| 请求／固定状态 | 封闭 payload、结果状态、机械依赖完整性 | `evaluation_contracts.py::RuleEvaluationRequest`、`evaluation_contracts.py::RuleEvaluationResult`；`evaluation_state.py::SnapshotBase` | Host 输入、各计算路径 |
| 预检与依赖 | 资格、当前机械状态、有效素材和支持性检查 | `evaluation_preflight.py::prepare_attack`；`evaluation_class_dependencies.py::operation_dependencies` | 角色、装备、法术等对应依赖模块 |
| 攻击与候选计算 | 用独立工作对象折叠攻击／伤害／回合事件 | `evaluation_attack.py::execute_attack`；`evaluation_computation.py::CombatComputation` | 共享 activities、效果与 Reaction 计算 |
| 其他已迁移切片 | 检定、豁免、物品、休息、移动、施法与关闭 | `evaluation_checks.py::evaluate_check`；`evaluation_spells.py::evaluate_invocation`；`evaluation_turn.py::evaluate_turn` | 各自 admission、计算和 Delta 投影 |
| 补充候选接口 | 新战斗初始化、集中伤害和开合检定 | `evaluation_initialization.py::evaluate_combat_initialization_candidate`；`evaluation_concentration.py::evaluate_concentration_candidate`；`evaluation_openable.py::evaluate_openable` | 显式专项请求；不是所有路径都经 evaluate 分发 |
| 结果投影／检查 | 将 before/after 投影为封闭、带原值保护的操作 | `evaluation_delta.py::StateDelta`；`evaluation_projection.py::attack_delta` | 结果验证、Host 最终采用 |
| RNG／阶段证据 | 显式随机状态转换及有范围的机械阶段证明 | `evaluation_rng.py::RNGContext`、`evaluation_rng.py::RNGTransition`；`evaluation_stage_validation.py::verify_stage_result` | 固定请求、候选事件及最终 Delta |
| 素材身份 | 固定有效语料、三字段绑定、默认共享实例 | `evaluation_ruleset.py::FrozenRuleset`、`evaluation_ruleset.py::freeze_ruleset` | Data AssetLoader、固定／可变加载器 |
| 共享规则解析器 | 类型化活动的攻击、伤害、检定、效果及公式解析 | `activities/resolver.py::resolve_activity`、`activities/context.py::ActivityResolutionContext` | rules、规则资产、显式上下文／RNG |
| 空间／交付／生命周期 | 几何、目标投送、区域和效果／反应协作 | `spatial.py::GridTopology`；`spell_delivery.py::plan_delivery`；`effect_lifecycle.py::OngoingEffectLifecycle` | 各 evaluator 与 Legacy 的相应适配路径 |
| SRD Data | 素材 Schema、可变／bundled 读取和冻结捕获 | `packages/dnd5e-srd-data/src/dnd5e_srd_data/loader.py::AssetLoader`；`packages/dnd5e-srd-data/src/dnd5e_srd_data/frozen.py::FrozenAssetLoader` | Engine、Bridge overlay；不持有 Session |
| Legacy combat | 原战斗 handle、注册表、工作状态及事件循环 | `orchestrator.py::start_combat`、`orchestrator.py::submit_player_intent` | live_*、共享算法、Legacy host handoff |
| Bridge | HTTP 输入、内容、本地 handle 和请求重放 | `packages/nat20-bridge/src/nat20_bridge/app.py::create_app`；`packages/nat20-bridge/src/nat20_bridge/state.py::BridgeState` | Engine Legacy API、内容加载／Homebrew |
| Demo | 场景选择、种子／日志重放和展示 | `apps/demo/src/nat20_demo/app.py::create_app` | Demo replay、Engine；不是 SM 持久宿主 |

模块职责由实际消费者决定：共享算法不等于共享权威状态，命名带 evaluation 也不代表
所有输入组合已支持。具体切片的 admission／依赖检查和真实测试是新增能力的阅读入口。

## 3. 代表性处理流程

### 一次 Stateless 求值

1. 调用 `evaluation.py::evaluate`，传入 `RuleEvaluationRequest` 和可选 loader。
   请求包含操作、固定 Snapshot、Session／命令关联、规则绑定及显式 RNGContext。
2. 请求经 deepcopy 和模型验证，保留固定输入隔离；选择显式素材或共享冻结默认素材，
   `evaluation_ruleset.py::verify_ruleset` 核对有效规则身份。
3. 分发到支持的 operation。攻击经预检；Check、Save、Rest、Item、Movement、
   Invocation、Turn、Closure 等切片各自验证完整必要机械依赖和支持范围。
4. 准入后才恢复／使用请求 RNG、支付候选资源并执行共享规则算法；工作对象只服务
   本次计算，不创建或恢复一个独立权威 Session／Legacy combat 作为永久适配前提。
5. 投影 guarded Delta、保序 proposed events、RNGTransition 和适用的 stage_facts。
   已接受操作的阶段覆盖可以明确为 uncovered，不能当成所有复杂机械链都已有阶段证明。
6. `RuleEvaluationResult.verify_request` 验证关联及对应结果不变量，再交回调用方。
   SM 还要重验权限／版本／原值／领域约束，自己持久提交和发布。

规则结果状态为 `accepted`、`rejected`、`unsupported` 或 `needs_choice`；
无效 Schema／绑定或内部不变量异常可以直接传播，不是所有失败都会变成普通拒绝。
`accepted` 只表示返回受支持的规则提案，从未表示世界已经改变。

当前 `evaluate` 产生保守的 world-version read fence；它不是每实体细粒度版本协议，
也不是执行全 Session 完整性审计。必要完整机械 Snapshot 检查仍须保留。

### 行动可用性与专项接口

`evaluation_availability.py::query_action_availability` 深复制／验证输入和绑定，
复用实际攻击预检，返回 `available`／`unavailable`／`unknown`。
此接口没有命令回执、请求 RNG、Delta 或资源支付，且只覆盖当前支持的查询形状；
它不替代外部认证，也不保证未来状态下仍可执行。

初始化、集中伤害和 Openable 使用各自显式专项请求与入口，读取对应合同和测试；
不能因为存在统一请求类型，就声称这些都已经成为同一个通用生产 dispatch。

### 规则素材准备与复用

1. `AssetLoader` 提供有效 typed assets；Bundled、Memory 或受控内容组合可以作为来源。
2. `freeze_ruleset` 捕获完整有效语料，验证枚举／身份、typed item 一致性和 UUID 唯一性，
   生成可信绑定；构造失败不发布半成品，来源在构造期间必须稳定。
3. FrozenAssetLoader 私有持有解析模型／索引，getter 返回防御深复制，供局部候选使用。
   只信任 Engine 创建的具体冻结类型，不信任任意 loader 自报的 binding 属性。
4. 默认 bundled dataset 在进程中成功构造后共享；自定义冻结集由持有引用者管理，
   不加入全局每 loader／Session 缓存。更换内容需要新 dataset，不原地修改旧集。
5. raw mutable loader 兼容路径仍完整核验／哈希有效素材；冻结素材复用不等于缓存规则结果。

Bridge 的 `packages/nat20-bridge/src/nat20_bridge/overlay.py::OverlayAssetLoader`
当前先查 base，再由 Homebrew 兜底，不能把“overlay”名字理解为覆盖 canonical 素材。

## 4. 数据、状态与结果归属

| 类别 | 持有／修改者 | 生命周期及边界 |
| --- | --- | --- |
| SM 持久事实和当前 RAM | SM 所属 Facts、Records、SessionMemory | RE 不读取 SM SQL，也不负责恢复、Receipt／Outbox 或 RAM 发布 |
| 固定输入 | Host 提供 Snapshot、绑定、显式 RNG | RE 隔离并校验；输入完整性不能靠重新查询外部世界补齐 |
| 本次候选 | Computation、activity context 和局部索引 | 计算期间修改，不跨请求作为权威世界保留 |
| Delta／事件／RNG 后继 | RE 生成，Host 验证采用 | 未提交提案；不能自行发送成 SM 的 committed WorldEvent |
| 规则素材 | FrozenRuleset／FrozenAssetLoader 私有模型和索引 | 经验证的内容身份和防御复制；不是持久世界状态 |
| Legacy 工作状态 | `orchestrator.py` 的 `_LiveCombat`／`_REGISTRY` | handle 跨调用保留；现存兼容路径，完整 Stateless 迁移仍未完成 |
| Bridge／Demo 宿主状态 | BridgeState 的 handles、锁、事件收集／重放回执；Demo replay | 进程内宿主行为，不是 SM 提交协议；Homebrew JSON 是内容存储 |

空间、效果、反应和区域模块中的“state”需看真实持有者：有些是固定 wire component，
有些是单次候选，有些属于 Legacy 工作对象，不能只凭类名判定持久化或无状态。
Legacy View 会复制集合容器，但部分模型值仍共享，应按实际只读消费约定处理。

## 5. 跨仓库接口

| 方向 | 当前交接 | 责任边界 |
| --- | --- | --- |
| SM→Stateless RE | RuleEvaluationRequest、CombatSnapshot／NonCombatSnapshot、RulesetBinding、RNGContext | SM 构造一致必要闭包；RE 计算，结果未提交 |
| RE→SM | RuleEvaluationResult、StateDelta、proposed_events、RNGTransition、read_set／stage_facts | SM 再验证并增量持久化，SQLite COMMIT 后受控发布 RAM |
| SRD Data→Engine | AssetLoader 及 schema typed assets→FrozenRuleset | 内容身份不是安装包版本；完整语料准入与普通请求成本分开 |
| Bridge／Demo→Engine | Legacy handles、PlayerIntent、构建／几何等实际 API | 不自动等于 SM 使用的 Stateless 输入输出或范围 |

Schema／evaluator 版本值查当前定义、[Stateless 合同](dev/stateless-evaluation.md)及测试；
不在总览中把易变数字写成永久约束。SM 的安装 pin 由其 `pyproject.toml`／`uv.lock` 决定，
与本 checkout、RE 自身支持范围及正式跨模块 ABI 批准状态分别核对。
上层[项目契约](https://github.com/agentic-trpg/agentic-trpg/blob/docs/code-documentation-and-skills/docs/MODULE_CONTRACTS.md)
中的目标类型／示例不能替代当前 DTO 或 SM 采用白名单。

## 6. 按任务查找代码

| 任务 | 首先阅读 | 后续阅读／验证 |
| --- | --- | --- |
| 新增规则求值能力 | evaluate 分发、对应 admission／依赖模块、请求／结果 DTO | 共享算法、候选投影、Delta／RNG／阶段验证；新增支持不能跳过拒绝路径 |
| 修改法术／效果／反应 | invocation 切片、activities、生命周期及交付模块 | [交付设计](dev/spell-area-delivery.md)、[效果生命周期](dev/effect-lifecycle.md)、实际 bound assets |
| 调整素材加载或规则绑定 | evaluation_ruleset、Data loader／frozen | [冻结素材合同](dev/stateless-evaluation.md)、mutable 路径与复制／UUID 测试 |
| 查看当前支持范围 | [能力矩阵](capabilities.md)、[迁移进度](dev/stateless-migration-progress.md) | admission 和真实测试；别把 Legacy 运行成功当 Stateless 支持 |
| 查看 Bridge／Demo | 各自 app、Bridge routes／combat_execution、Demo replay | HTTP／重试与宿主状态边界；不扩大 SM 世界提交能力 |
| 同步跨库接口或维护说明 | [源码说明规范入口](dev/source-documentation.md)、本页 | sibling trpg-contract-sync；本地 trpg-code-documentation |

## 7. 维护与证据边界

职责、公开入口、状态归属或跨仓库接口改变时同步本页；函数细节归 docstring，
模块背景归模块说明，正式规则和目标架构归权威合同，简单局部修改无需机械改本页。
本页是当前源码导航，不是整库规则、恢复、并发、性能或自动 Skill 触发的验收证明。
完整 Legacy 迁移、通用机制、Host 集成及目标 ABI 中的 OPEN 项保留原状态。
本批静态核对和实际检查见[已有验收记录](dev/source-documentation-acceptance.md)。

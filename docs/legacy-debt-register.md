# Legacy Debt Register

> Status: active
> Last verified: 2026-07-14
> Scope: known legacy concepts, deprecated paths, migration state, and cleanup rules.

本文登记历史残留。目的不是列 TODO，而是防止旧概念在新实现里反复复活。

## 1. 使用规则

每条旧债至少包含：

- `状态`: active / migrating / fixed / deprecated
- `旧理解`: 历史上怎么理解
- `当前口径`: 现在应如何理解
- `影响面`: 代码、配置、测试、文档、前端、运行态
- `收口方式`: 保留兼容、迁移、删除或观察

状态含义：

| 状态 | 含义 |
|---|---|
| `active` | 仍可能影响系统，需要继续处理 |
| `migrating` | 已有新路径，但旧路径还在兼容 |
| `fixed` | 已完成修复，只保留追溯 |
| `deprecated` | 明确废弃，不应新增依赖 |

## 2. 因子系统旧债

### BB 作为过滤器

- 状态: `fixed`
- 旧理解: `bb_width` 是硬过滤器或特殊过滤条件。
- 当前口径: `bb_width` 是 volatility context 因子，不参与方向评分，不接入 ExecutionGate 硬过滤。
- 影响面: `PortfolioCompositor`、readiness、AWE、前端因子投票、旧测试注释。
- 收口方式: 保留旧字段兼容账本；新展示使用 `role=context` 和 `used_in_score=false`。

### 固定 70/30 战术/宏观投票

- 状态: `fixed`
- 旧理解: 组合分数固定按 tactical 70%、macro 30% 混合。
- 当前口径: 只在两类 alpha 都存在时按配置混合；只有一类 alpha 时权重归一为 1。
- 影响面: `PortfolioCompositor`、测试、账本解释。
- 收口方式: 保留 `tactical_score/macro_score` 兼容字段，新增 `alpha_score` 真实表达方向分。

### 事件和日历因子表达方向

- 状态: `fixed`
- 旧理解: FOMC/NFP/hour/day 可以直接映射看多或看空。
- 当前口径: 事件、日历、时段是 context/gate/sizing 语义，不直接投方向票。
- 影响面: 因子角色、事件 sizing、前端投票、学习归因。
- 收口方式: 方向评分只读 alpha；事件风险通过 gate/sizing/context policy 生效。

### 低频因子重复污染归一化历史

- 状态: `fixed`
- 旧理解: 所有因子每根 bar 都同样写入 normalizer 历史。
- 当前口径: 低频因子按 cadence 和 history_sample_policy 采样，值未变化时不重复污染 rank/zscore。
- 影响面: `SignalNormalizer`、外部因子、回测/live 对齐。
- 收口方式: 保留默认 `bar/every_bar` 兼容；宏观/COT/ETF/事件使用低频策略。
- 验证方式: `tests/test_signal_normalizer.py::test_low_frequency_factor_history_samples_only_on_value_change`、`tests/alpha/test_low_frequency_factors.py`。

### shadow/discovered/live 混用

- 状态: `fixed`
- 旧理解: 搜索出来的因子容易被直接注册或进入主链。
- 当前口径: shadow 永不直接交易；discovered 必须经证据门槛和治理晋升；live 才进入主链路。
- 影响面: discovery、registry、runtime selection、AWE、readiness、Catalog。
- 收口方式: `runtime_factor_selection` 返回 selected/excluded/reason；治理动作由 Orchestrator 执行。
- 验证方式: runtime selection 明确排除 `shadow_only` / `lifecycle_dead`，Factor Governance 是唯一 lifecycle executor。

## 3. 自治治理旧债

### V16 后验只进入学习账本，没有进入元大脑调度

- 状态: `fixed`
- 旧理解: `supervisor_counterfactual_review` 只供复盘/自主学习使用，V16 只生成只读快照，结论不会定向到具体智能体；入场亏损和监督器过早干预可能被混成一个“失败”。
- 当前口径: `BrainMemoryService` 读取成熟后验，`build_posterior_arbitration()` 按因果范围分离 entry 与 supervisor；`V16BrainOrchestratorService` 将最高证据结论写入 `v16_brain_command` 并定向给专员。V16 只能判断、排序、要求证据和下达交接命令，实际 policy/runtime/权重/模板变更仍由专员和既有 Governor 经过风控与回滚边界执行。
- 影响面: V16 brain state/memory/plans/evals、`brain_governance_candidate`、demo nursery、agent scorecard/briefing、readiness 和 Web V16 页面。
- 收口方式: demo runner 在 review/bridge/apply 前自动调用 V16 编排；命令以 posterior fingerprint + scope/action 幂等；`/api/ops/brain/commands` 和 readiness `v16_brain_orchestration` 暴露闭环状态；缺少后验源表时标记 `posterior_source_missing`，不伪造健康闭环。
- 验证方式: `tests/test_v16_brain_orchestrator.py`、`tests/test_v16_read_only_brain.py`、`tests/test_autonomous_evolution_cycle.py`。

### V16 委派命令没有成为专员 mutation 的统一闸门

- 状态: `fixed`
- 旧理解: V16 虽然能写 `v16_brain_command`，但部分 Governor、模板同步和自主学习路径仍可自行改 runtime/权重。
- 当前口径: 生产环境中 `autonomous_learning`、`factor_governance`、`position_supervisor_governance` 的扩张性 mutation 必须由 `V16CommandGate` 校验近期、目标和 scope 一致的 delegate 命令；V16 只判断/指挥，专员负责执行。rollback、reduce、tighten 属于风险收紧例外。
- 影响面: `v16_brain_command`、runtime mutation、factor governance、parameter templates、position supervisor、Agent Authority。
- 收口方式: 缺命令 fail-closed，并继续执行 `RiskPolicyService`、`DecisionPolicy`、`RuntimeConfigMutationService`；新增专员必须先登记 Agent Authority contract。
- 验证方式: `tests/test_agent_coordination_fixes.py::test_v16_command_gate_fails_closed_and_accepts_recent_delegate`。

### 全局实验预算被批量准入绕过

- 状态: `fixed`
- 旧理解: 每个候选单独 admission，批量运行可能在同一时刻超过全局 24 个实验预算。
- 当前口径: `learning_experiment_reservation` 在单事务/锁内原子预留槽位，应用失败会释放，成功 prepare 后结算；全局 active 统计包含 reservation。
- 验证方式: `tests/test_agent_coordination_fixes.py::test_batch_reservation_is_atomic_and_bounded`。

### 候选审查重复和过期候选持续占用队列

- 状态: `fixed`
- 旧理解: 相同 evidence 每轮生成新的 review，旧 active candidate 没有统一 TTL。
- 当前口径: candidate 默认 TTL 24 小时；review 使用 evidence fingerprint 幂等去重，过期 active candidate 自动标记 `superseded/expired`。
- 验证方式: `tests/test_agent_coordination_fixes.py::test_candidate_review_is_idempotent_and_expiry_is_reconciled`。

### Proposal Registry 把维护事件和重复投影当作可行动建议

- 状态: `fixed`
- 旧理解: Registry 读模型会积累重复建议、维护动作和历史噪音，掩盖真实可行动 proposal。
- 当前口径: 按 source/scope/action 压缩投影，维护型动作不进入 actionable projection；原始来源账本不删除，始终可重建。
- 验证方式: `tests/test_agent_coordination_fixes.py::test_proposal_projection_compacts_without_deleting_source_ledger`。

### 自主变更统一标记为人工 API mutation

- 状态: `fixed`
- 旧理解: system actor 的自动变更仍写 `manual_api_mutation`，导致 scorecard、后验归因和治理审计失真。
- 当前口径: `MutationAudit` 按 source agent/actor 写入 `autonomous_mutation`；真实人工调用保留 `manual_api_mutation`。
- 验证方式: `tests/test_agent_coordination_fixes.py::test_mutation_audit_distinguishes_autonomous_and_manual`。

### AWE 权重变更未进入后验账本

- 状态: `fixed`
- 旧理解: AWE 只要经过 DecisionPolicy、RiskPolicy 和 runtime mutation 就算完整闭环，可不写 learning application/effect。
- 当前口径: AWE 与 Factor Governance 共用学习实验准入；每个实际生效的 AWE factor patch 必须记录 application/effect，readiness 交叉检查 mutation run 覆盖，历史缺口单列 legacy。
- 影响面: live AWE scheduler、权重 mutation、效果归因、readiness、Learning Web。
- 收口方式: `LearningExperimentAdmissionService` + `RuleEvolutionGovernor.log_application()`；同 scope active window 阻止新权重实验。
- 验证方式: `tests/test_evolution_closure_fixes.py`、`tests/test_learning_experiment_admission.py`。

### experience_priors 只有接口没有生产输入

- 状态: `fixed`
- 旧理解: DecisionPolicy 声明 `experience_priors` 参数就代表经验已经反哺权重决策。
- 当前口径: prior 必须由 `ExperiencePriorService` 从 terminal bounded effects 构造，并显式传入全部三个生产 `fast_decide` 调用；原始 memory 或 mixed/inconclusive effect 不能直接影响权重。
- 影响面: AWE、Factor Governance、trade review regime、effect ledger、Learning Web。
- 收口方式: review 保存 entry regime；历史 regime 从 decision ledger 事实回填；prior 有样本、置信度、时效衰减和 0.85~1.15 硬边界。
- 验证方式: `tests/test_learning_experiment_admission.py`、`tests/test_trade_reviewer.py`、`tests/test_learning_backfill.py`。

### discovered 冷尾部自动进入 live

- 状态: `fixed`
- 旧理解: registry 中所有非 DEAD discovered factor 都应自动加入 live engine/compositor，即使没有显式 runtime config。
- 当前口径: 显式配置因子优先；额外 discovered 受 runtime budget 限制并按 lifecycle/score 选择，剩余候选保留研究/registry 证据但不进入 live 方向组合。
- 影响面: StreamingFactorEngine、PortfolioCompositor、decision snapshot、AWE、readiness。
- 收口方式: `alpha.runtime_factor_selection` 默认 discovered budget 24，`_merge_portfolio_configs` 不再因冷尾部 weight row 自动激活因子。
- 验证方式: `tests/alpha/test_runtime_factor_selection.py`、`tests/test_live_service_tick.py`。

### runtime snapshot 相同事件重复写入

- 状态: `fixed`
- 旧理解: 同一 source/run 对完全相同 config 的重复 persist 也必须增加 snapshot version。
- 当前口径: 连续同 hash + 同 source + 同 run_id 复用最近 snapshot；不同事件来源或 run 仍建立独立回滚点。
- 影响面: startup、parameter sync、factor governance audit、overlay transaction、readiness。
- 收口方式: `persist_runtime_config_snapshot()` 事务内幂等；不删除历史快照。
- 验证方式: `tests/test_runtime_config.py`、`tests/test_factor_autonomy_hardening.py`。

### policy_suggestion 作为人工审批队列

- 状态: `fixed`
- 旧理解: `policy_suggestion` 默认等待人工审批。
- 当前口径: 它是自治建议和执行审计表，状态应归一到自治语义。
- 影响面: learning API、evolution orchestrator、前端治理页面。
- 收口方式: 使用 normalized status：`proposed/auto_approved/applied/rolled_back/blocked_by_risk/superseded`。
- 验证方式: `backend.services.policy_suggestion_status` 统一转换历史 raw status；readiness、计数和 Proposal Registry 只按 normalized status 判断自治语义，历史 raw 值仅保留审计兼容。

### 自治配置直接 patch 内存

- 状态: `fixed`
- 旧理解: 自动治理可以直接修改 RuntimeConfig 内存。
- 当前口径: 自治配置必须写 DB overlay 和 runtime_config_snapshot，重启可恢复；长期运行进程通过 `runtime_config.shared()` 低频吸收最新 overlay，避免 writer 进程和 reader 进程配置漂移。
- 影响面: AWE、参数模板、FactorGovernanceOrchestrator、startup。
- 收口方式: 统一使用 `RuntimeConfigMutationService` / `RuntimeConfigOverlayService`。

### overlay 非事务发布与并发全量覆盖

- 状态: `fixed`
- 旧理解: overlay 写入前可先替换进程内 RuntimeConfig，多个自治任务可各自读取并写回全量权重。
- 当前口径: overlay row 与 snapshot 同事务提交、提交后发布；写事务使用跨进程锁，长期进程从 YAML base + 完整 overlay 重建，自治 producer 只写局部 patch。
- 影响面: AWE、FactorGovernance、startup/refresh、clear/rollback。
- 收口方式: `RuntimeConfigOverlayService._mutate_overlay` + registered overlay base + empty-overlay refresh。
- 验证方式: `tests/test_factor_autonomy_hardening.py` 的 concurrent/failed transaction/empty overlay 测试。

### Evolution 与 Factor Governance 双生命周期执行者

- 状态: `fixed`
- 旧理解: Evolution Canary 可以直接 promote/rollback/retire，同时 Factor Governance 也执行同类动作。
- 当前口径: Evolution 只生产 shadow/Canary/retirement evidence 和 candidates；FactorGovernance 是唯一 lifecycle scheduler executor。重型任务由 `EvolutionWorkCoordinator` 跨进程串行。
- 影响面: registry source、runtime factor config、Canary、学习审计、scheduler。
- 收口方式: 移除 scheduled evolution 的直接 lifecycle 调用，Canary regression 由 FactorGovernance 经 RiskPolicy 执行。
- 验证方式: `tests/test_evolution_closure_fixes.py`、`tests/test_evolution_work_coordinator.py`。

### builtin quarantine 只有隔离没有自动恢复

- 状态: `fixed`
- 旧理解: Factor Governance 可以把弱 live 因子置为 `enabled=false`，但后续只能靠人工或 AWE 恢复权重，无法重新进入 live selector。
- 当前口径: `QUARANTINE` builtin alpha 因子满足冷却期、健康证据、样本新鲜度和模型弱势清除条件后，由 Factor Governance 在 demo 自治周期自动恢复；`RETIRED/DEAD` 和 discovered Canary 生命周期不被该入口反向复活。
- 影响面: Factor Catalog、runtime factor selection、Factor Governance、RiskPolicy、runtime overlay/snapshot、治理审计。
- 收口方式: 新增 `restore_factor_live` RiskPolicy 动作和受控 runtime mutation；重复失败的 live 因子始终重新进入 `QUARANTINE`，避免恢复后状态变成不可恢复的 `ACTIVE + disabled`。
- 验证方式: `tests/test_factor_governance_recovery.py`。

### Canary 重复消费同一 OOS 窗口

- 状态: `fixed`
- 旧理解: 每小时重复计算相同 aggregate OOS bars/PnL 也可以连续推进多个 Canary stage。
- 当前口径: shadow performance 保存 dataset/evidence hash、window watermark 和 new bars；同一 hash 只能评估一次，阶段间必须累计 fresh evidence。
- 影响面: `alpha.shadow_trader`、`deployment.canary`、`canary_state`、Factor Catalog。
- 收口方式: evidence watermark + `STAGE_MIN_FRESH_EVIDENCE`。
- 验证方式: `tests/test_evolution_closure_fixes.py::test_canary_requires_new_evidence_between_stage_promotions`。

### experiments 表双 schema

- 状态: `fixed`
- 旧理解: `ExperimentTracker` 可用 JSON blob schema，`EvolutionExperimentRegistry` 可在同一库使用 structured schema。
- 当前口径: `data/experiments.db` 只有 structured canonical schema；未接线的 Evolution registry adapter 已移除，旧 blob 自动原位迁移。
- 影响面: experiments API、周报、GP/模型实验记忆、db doctor。
- 收口方式: 统一到 `research.experiment_tracker.ExperimentTracker`。
- 验证方式: `research.experiment_tracker.ExperimentTracker` 的 API/报告回归测试。

### 旧 RegimeAwareClassifier

- 状态: `retired`
- 旧理解: `alpha/regime_classifier.py` 的 LogisticRegression 可以作为 regime 条件下的开仓判断。
- 退役原因: 旧 artifact 只保存 pickle 模型和 trained 标记，缺少固定特征 schema、PIT 数据窗口、OOS/replay、校准与权限证据；当前代码产生 15 个特征，旧模型仅接受 9 个特征，无法安全推理。
- 当前口径: regime 事实使用 `risk.regime.RegimeDetector` 和 `backend.services.market_regime.resolve_market_regime`；不恢复旧 pickle。未来如建模，必须使用固定特征 schema 和成熟后验样本，先进入 shadow/context advisory，且只允许收紧风险。

### 回滚临场推断

- 状态: `fixed`
- 旧理解: 回滚时根据当前状态推断应该恢复什么。
- 当前口径: 回滚只能使用当时 decision 的 `rollback_json.runtime_config`。
- 影响面: Orchestrator、learning_application_effect、RiskPolicyService。
- 收口方式: 负后验触发回滚前先过 `rollback_factor_action` 风控。

### mixed 后验永久冻结

- 状态: `fixed`
- 旧理解: `learning_application_effect=mixed` 是最终状态，效果协调器只继续扫描 applied/observing/effective。
- 当前口径: mixed 是有冷却的待复评状态，使用最新可比样本继续判断；超过观察窗仍无法归因时收口为 `inconclusive`，后续只能通过新 application 重试。
- 影响面: `RuleEvolutionGovernor`、Factor Governance pending gate、Agent Scorecard、Proposal Registry。
- 收口方式: bounded reconcile + mixed cooldown + observation timeout；不直接改权重，仍由既有 rollback/reinforce 路径处理最终后验。

### YAML 与自治 overlay 差异误报 runtime drift

- 状态: `fixed`
- 旧理解: readiness 直接比较 YAML RuntimeConfig 与内存 singleton，合法持久化 overlay 也会被判定为 drift。
- 当前口径: drift 比较 YAML base + persisted overlay 的有效配置与内存；base/overlay 差异单独展示，不作为异常。
- 影响面: backend readiness、重启恢复、自治 posture。
- 收口方式: `config_runtime_drift()` 读取 overlay authority，同时保留 disk/effective/memory execution semantics 供排障。

## 4. 数据和运行态旧债

### SQLite state.db 作为运行态主库

- 状态: `deprecated`
- 旧理解: `data/state.db` 可作为 live 状态库或排障入口。
- 当前口径: PostgreSQL `state_v1` 是运行态状态与学习审计主库。
- 影响面: 脚本、排障、测试、迁移说明。
- 收口方式: 生产路径不新增 SQLite state；只读查询用 `scripts/state_query.py` 或 PG 连接入口；状态库说明收敛到 `docs/state-postgres-store.md`；旧 `state-dual-write-postgres.md` 文件名和 `scripts/state_dual_write_status.py` 占位脚本已清理。

### 独立 L2 collector

- 状态: `deprecated`
- 旧理解: 用独立 Open API 连接采集 L2。
- 当前口径: L2 整体采集链已于 2026-07-11 退役；depth 代码、配置、风控字段和历史数据库均已删除。
- 退役原因: 当前 cTrader depth size 为固定对称档位，`imbalance` 恒为 0，不具备真实流动性/订单流语义。
- 影响面: systemd、scripts、运行 SOP。
- 收口方式: 不恢复 `quant-l2-collector.service` 和旧脚本。

### 旧 Web Console / H5 web-view 路线

- 状态: `deprecated`
- 旧理解: 小程序通过 web-view 或旧 H5 console 承载复杂展示。
- 当前口径: 新 Web 前端承接完整操作台，小程序保留轻量状态。
- 影响面: `miniprogram_v2`、`web_frontend`、Caddy、前端规划。
- 收口方式: 新复杂能力优先 Web，不要求小程序配置 web-view 业务域名。

### 临时前端 API smoke 脚本

- 状态: `deprecated`
- 旧理解: 用 `scripts/check_*.py`、`scripts/quick_check.py`、`scripts/cross_validate.py`、`scripts/start_and_check.py`、`scripts/test_fe*.py` 直接登录本地后端检查旧 `/api/state` 字段。
- 当前口径: 前端/API 验证应走正式测试、`/api/ops/backend-readiness`、`/api/live/*` 和 Web 前端 contract，不保留硬编码口令的临时脚本。
- 影响面: scripts、凭据安全、旧 `/api/state` 理解。
- 收口方式: 已删除这些临时脚本；后续新增 smoke 脚本必须读取环境变量或使用正式测试 fixture，不得写入明文口令。

### scripts 目录内历史回测输出

- 状态: `deprecated`
- 旧理解: `scripts/data/charts/backtest_v4_result.json` 和 `backtest_v4_trades.jsonl` 可以跟源码一起保留，作为历史示例结果。
- 当前口径: scripts 目录只保留可执行维护脚本；回测输出、交易明细和模型报告属于运行产物，应进入 ignored `data/charts/`、报告目录或外部归档。
- 影响面: scripts、文档审计、下一版性能基线设计。
- 收口方式: 已删除 scripts 下历史回测输出；后续性能基线必须由可复现脚本生成，不读取静态旧结果。

### scripts/debug 临时探针

- 状态: `fixed`
- 旧理解: debug 目录可长期保留一次性导入、GitHub 搜索、Windows 启动和本地 cTrader 验证脚本。
- 当前口径: debug 目录不是稳定运维入口；一次性探针、硬编码 Windows 路径和联网搜索脚本会污染下一版架构判断。
- 影响面: scripts、Windows 本地旧工作区、已退役 tick 采集、cTrader 验证。
- 收口方式: 已删除 `scripts/debug` 临时入口；Dukascopy 历史 tick 采集于 2026-07-11 完全退役，正式排障只使用受维护脚本和服务日志。
- 验证方式: 仓库不再包含 `scripts/debug` 业务脚本，部署/SOP 不引用该目录。

### 旧 cloud_deploy / docker-compose 打包路线

- 状态: `deprecated`
- 旧理解: `scripts/pack_for_cloud.py` 可以把 DuckDB 数据和 `docker-compose.yml` 打包上传云服务器。
- 当前口径: 当前后端运行以服务器 systemd、Caddy、PostgreSQL state、DuckDB 月库为准；仓库没有维护 `docker-compose.yml` 作为部署事实源。
- 影响面: 部署文档、数据打包、服务器 SOP。
- 收口方式: 已删除 `scripts/pack_for_cloud.py`；后续如要容器化，必须重新设计并写入正式部署文档，不能复用旧 cloud_deploy 脚本。

### legacy AWE trailing 保护候选

- 状态: `fixed`
- 旧理解: AWE trailing 可以作为独立持仓保护执行器直接驱动止损。
- 当前口径: 它只构造 legacy trailing 保护候选，并由统一持仓保护仲裁处理；不能绕过 `PositionSupervisor`、holding timeout 或 `RiskPolicyService`。
- 影响面: `backend/services/live_service.py`、`backend/services/live_position_lifecycle.py`、持仓保护 trace、历史 close reason、测试兼容。
- 收口方式: legacy 计算仅作为 `ProtectionCandidate` 兼容适配器保留，统一由持仓保护仲裁、`PositionSupervisor` 和 `RiskPolicyService` 决定是否执行；它不再拥有独立执行权。历史 close reason 映射继续只用于复盘兼容。
- 验证方式: `tests/test_live_position_lifecycle.py` 覆盖候选生成，`tests/test_live_service_lifecycle.py::test_legacy_awe_trailing_records_protection_state_not_supervisor_cooldown` 覆盖统一仲裁边界。

### legacy parameter sweep

- 状态: `fixed`
- 旧理解: 旧参数网格扫描可以直接 patch runtime config。
- 当前口径: live 服务不再保留 `_scheduled_param_tune()` 或 param tune 状态写入口；运行参数切换必须走 parameter template、governance、risk policy 和 runtime overlay。
- 影响面: `backend/services/live_service.py`、参数模板、evolution closure tests。
- 收口方式: 已确认没有 scheduler/API 生产调用后删除旧 sweep、JSON/DB 状态写函数；保留架构测试确保入口不会复活。
- 验证方式: `tests/test_evolution_closure_fixes.py::test_legacy_param_tune_entrypoint_is_removed`。

### legacy PreTrade/CircuitBreaker live 误用

- 状态: `fixed`
- 旧理解: `risk/pre_trade.py`、`risk/circuit.py` 或旧 `execution/router.py` 可以作为 live 主链路的开仓授权/熔断事实源。
- 当前口径: 当前 live 主链路由 `RiskPolicyService.evaluate(...)` 统一授权；账户/运行态阈值由 `RiskLimitSnapshot` 输入 `RiskGovernor`；live loop 的日内 circuit breaker 只是执行快停保护，阈值同样来自 `RiskLimitSnapshot`。
- 影响面: `risk/pre_trade.py`、`risk/circuit.py`、已移除的 legacy execution router、`backend/services/live_service.py`、`risk/policy_service.py`、测试、文档。
- 收口方式: 旧模块已在代码注释中标为 paper/backtest/legacy；live 不新增这些模块的调用；VaR/CVaR、事件风险、模型权限和开仓/改仓/治理动作均回到 `RiskPolicyService`。
- 验证方式: `tests/risk/test_policy_service.py`、`tests/test_live_service_circuit.py`、`tests/alpha/test_execution_gate.py`、`tests/test_live_loop_shell.py`。

### live 决策使用过旧或未闭合 K 线

- 状态: `fixed`
- 旧理解: live tick 只要本地 bars 月库有最近几根 bar，就可以直接用最后一根作为 `complete=true` 决策输入；spot quote 可以修正执行价格，数据延迟只靠宽泛 `data_lag_max_seconds` 兜底。
- 当前口径: live 因子决策只能使用最新已闭合 bar。`classify_bar_freshness()` 按 timeframe 推导应有闭合 bar；`_ensure_live_decision_bars_fresh()` 会在因子计算前过滤未闭合 bar，缺 bar 时通过主 cTrader bridge 回补月库并重载。修复失败时只阻断 open_trade；同一根已闭合 bar 只允许推进一次 signal/open，重复 tick 只运行持仓观察/保护；即使 bar fresh，`RiskPolicyService` 也会在信号 age 超过 `max(180s, 1.5 * timeframe)` 时以 `decision_signal_age_stale` 阻断开仓，持仓监督和平仓链路继续运行。
- 影响面: `backend/services/live_data_sync_helpers.py`、`backend/services/live_service.py`、`backend/services/live_position_lifecycle.py`、`risk/policy_service.py`、同步健康、决策审计、学习标签解释。
- 收口方式: 新增 `decision_bar_freshness.v1` 运行态快照、`last_processed_decision_bar_ts` 和 `decision_freshness` 风控上下文；`StreamingFactorEngine` 拒绝重复/倒序 bar；`RiskPolicyService.evaluate("open_trade")` 对 `fresh=false` 返回 `decision_bar_stale`，对过期信号返回 `decision_signal_age_stale`，不改变 close/reduce/tighten 降风险动作。
- 验证方式: `tests/test_live_data_sync_helpers.py`、`tests/test_live_service_lifecycle.py::test_ensure_live_decision_bars_repairs_from_primary_bridge`、`tests/risk/test_policy_service.py::test_open_trade_blocks_stale_decision_bar_freshness`。

### 交易复盘把信号时间当实际入场时间

- 状态: `fixed`
- 旧理解: `trade_outcome_review.entry_ts` 可以直接使用 open decision 的 K 线时间，亏损归因主要按信号/因子/持仓质量解释。
- 当前口径: `entry_ts` 以 `order_lifecycle_event.filled` 的实际成交时间优先；信号 K 线时间保留为 `signal_bar_ts`。复盘必须记录 `entry_timing_context`、`decision_freshness_context` 和 `system_issue_context`，先识别数据时效/信号到成交延迟污染，再判断因子或持仓问题。
- 影响面: `alpha/reflection/reviewer.py`、`backend/services/review_contract.py`、`backend/services/failure_taxonomy.py`、`backend/services/autonomous_learning.py`、`research/factor_governance_lightgbm.py`、学习样本、因子治理训练、复盘 UI 摘要。
- 收口方式: 系统污染样本降为 partial/低权重；`factor_contribution_review` 标记 `system_contaminated` 并降低 confidence；`backfill_trade_review_timing_and_system_markers()` 只从现有 ledger/order/review 事实回填旧 review。
- 验证方式: `tests/test_trade_reviewer.py::test_trade_reviewer_separates_signal_and_fill_time_for_system_contamination`、`tests/test_autonomous_learning.py::test_backfill_trade_review_timing_marks_system_contamination`、`tests/test_factor_governance_lightgbm.py::test_factor_governance_lightgbm_skips_system_contaminated_reviews`。

### 多条权重写链各自拼装治理门

- 状态: `fixed`
- 旧理解: AWE、Factor Governance 和 Evolution/manual govern 可以各自调用 DecisionPolicy、RiskPolicy 与 RuntimeConfigMutationService，只要局部链路看起来完整即可。
- 当前口径: 所有实际因子权重变更统一由 `FactorWeightChangeService` 编排；每次都包含有界经验先验、共享实验准入、RiskPolicy、prepared application、runtime mutation 和后验观察。
- 影响面: live AWE、因子降权/晋升、manual govern API、Evolution、学习效果账本。
- 收口方式: 三条生产路径已迁入统一业务用例；prepared application 在 mutation 前落库，同 scope prepared 也属于 active experiment。
- 验证方式: `tests/test_factor_weight_change_service.py`、`tests/test_evolution_closure_fixes.py`。

### learning worker 导入 live_service 巨石

- 状态: `fixed`
- 旧理解: worker 可以复用 `live_service` 内的 feature engineering 和休市模型函数，即使这会同时加载 live 执行依赖和进程内状态。
- 当前口径: 学习研究任务属于 `backend.services.learning_research_jobs`；worker 不导入 live_service，休市判断消费跨进程 runtime health projection。
- 影响面: worker 启动内存、循环依赖、live/learning 权责边界、离线任务测试。
- 收口方式: live 仅保留兼容调度 wrapper，worker 直接注册独立 research job。
- 验证方式: `tests/test_factor_autonomy_hardening.py::test_learning_worker_registers_factor_governance_job`、`tests/test_offmarket_high_load.py`。

### 健康接口各自推测 broker 状态

- 状态: `fixed`
- 旧理解: `/api/health`、readiness 和 worker 可以从各自进程状态推断 cTrader/market session；拿不到时长期返回 unknown。
- 当前口径: live 进程发布 `runtime_health_projection.v1`，所有跨进程消费者读取同一新鲜度受控投影；投影只做展示/调度判断，不授权交易。
- 影响面: health API、backend readiness、离线深度学习调度、运维观察。
- 收口方式: 新增 `RuntimeHealthProjectionService`，live market-session snapshot 负责发布，消费者统一读取。
- 验证方式: `tests/test_runtime_health_projection.py`、`tests/test_backend_health.py`、`tests/test_backend_readiness_contract.py`。

### V16 页面同时承载解析、列表和容器编排

- 状态: `fixed`
- 旧理解: V16 自治页的字段解析、治理链展示和页面请求编排可以长期放在一个 1300+ 行组件中。
- 当前口径: 页面保留查询/动作编排，纯展示、状态映射和治理列表归 `features/v16/V16BrainViews.tsx`；现有视觉 token、响应式 CSS 和 API contract 不改变。
- 影响面: Web V16 页面可维护性、组件复用、首屏行为与交互回归。
- 收口方式: 按容器/展示边界拆分，页面文件降到约 600 行，并通过前端 architecture、typecheck 与 production build。
- 验证方式: `npm --prefix web_frontend run test`、`typecheck`、`build`。

### readiness 请求同步重建完整治理图

- 状态: `fixed`
- 旧理解: readiness 缓存失效后可以在 HTTP 请求内同步调用所有治理、回放、学习和 V16 聚合器。
- 当前口径: 请求只消费 `backend_readiness_snapshot.v1` 持久化投影；过期时启动单例后台刷新，冷启动快速返回 `warming_snapshot`。
- 影响面: API 延迟、backend 峰值内存、Web 操作台首屏、自治状态展示。
- 收口方式: `BackendReadinessSnapshotService` 统一 publish/latest/refresh，backend 单例后台构建，API 只读取投影；learning worker 不承担该构建，避免 broker/live 依赖越界。
- 验证方式: `tests/test_backend_readiness_snapshot.py`、`tests/test_backend_readiness_contract.py`。

### V16 delegate 可被多个 worker 重复使用

- 状态: fixed
- 旧理解: 近期 delegate 只做 read-only authorize，执行者自行决定何时应用，同一命令可能被重复消费。
- 当前口径: `v16_brain_command` 记录 claim/consume 状态、token、过期时间、apply count 和 posterior/evidence fingerprint；`V16CommandGate` 原子 claim，`RuntimeConfigMutationService` 在变更尝试前 consume，单条命令最多一次。
- 影响面: V16 委派可靠性、并发 worker、后验与参数变更的时间一致性。
- 收口方式: PostgreSQL additive migration + SQLite 兼容迁移；过期 claim 自动释放，已消费命令 fail-closed。
- 验证方式: `tests/test_agent_coordination_fixes.py::test_v16_command_claim_is_single_use_and_evidence_bound`。

### 治理配置入口依赖调用方记得传 V16 flag

- 状态: fixed
- 旧理解: `RuntimeConfigMutationService.apply_patch(require_v16_command=False)` 的默认值让新增系统调用可能绕过元大脑。
- 当前口径: 生产状态库上按 patch/source/action 自动识别治理 mutation；restore、incident 收紧和风险收紧例外保持边界，普通治理调用不能通过显式 false 绕过。
- 影响面: factor weight、factor signal、parameter template、position supervisor template、context policy。
- 收口方式: 中央推导 gate + claim/consume；所有下游仍必须经过 RiskPolicy/DecisionPolicy。
- 验证方式: runtime mutation、V16 gate 和治理回归测试。

### Factor Governance 与 Autonomous Learning 重复执行参数模板

- 状态: fixed
- 旧理解: FactorGovernanceOrchestrator 和 autonomous_learning 都可能激活模板并同步 runtime overlay。
- 当前口径: Factor Governance 保留证据读取和 handoff 审计，Autonomous Learning 作为参数/上下文模板执行 owner；Evolution 只产出 canary evidence/candidate，不再写 RegistryAdapter lifecycle。
- 影响面: 参数模板、学习应用、runtime overlay、重复 application/effect。
- 收口方式: 保留智能体，收敛 execution owner；模板和 supervisor 模板统一进入实验 reservation。
- 验证方式: parameter template、evolution closure、factor governance recovery 回归测试。

### 模板实验不进入统一 24 槽位预算

- 状态: fixed
- 旧理解: 24 槽位只约束 factor weight，模板切换可能绕过 effect backlog 和全局预算。
- 当前口径: parameter template 和 position supervisor template 使用 `LearningExperimentAdmissionService.reserve_scope`，同 scope 替换只复用一个 active effect 槽位，失败和过期 reservation 可回收。
- 影响面: demo 自主学习吞吐、effect 后验窗口、跨智能体实验竞争。
- 收口方式: 原子 reservation + finalize/release + application/effect 审计。
- 验证方式: `tests/test_learning_experiment_admission.py`、参数模板和 supervisor template 回归测试。

### 学习调度重复消费同一批历史事实

- 状态: `fixed`
- 旧理解: 每 30 分钟完整重建全部学习样本有助于持续进化，即使源事实没有变化。
- 当前口径: 自主学习调度以五类事实水位决定是否运行；成功后才提交 watermark，手动完整周期不被禁止。
- 影响面: learning worker CPU/内存、样本重复写、治理队列吞吐、实验后验窗口。
- 收口方式: 新增事实 watermark，并为 active application/effect 增加默认 24 个全局实验预算；worker 启动同时归档中断的 stale run。
- 验证方式: `tests/test_learning_cycle_watermark.py`、`tests/test_learning_experiment_admission.py`。

### Supervisor 反事实整条记录提前成熟

- 状态: fixed
- 旧理解: 只要存在未来 bar，就可用最后值补齐所有 horizon 并标记整条记录成熟。
- 当前口径: 每个 horizon 独立记录 expected/observed bars、窗口边界、最新 bar、成熟状态和数据指纹；60 分钟前不可治理。
- 影响面: supervisor template、policy suggestion、learning effect、Canary readiness。
- 收口方式: JSON v2 兼容扩展，旧字段继续读取，旧污染证据不得进入 active application。
- 验证方式: `tests/test_supervisor_counterfactual.py`。

### Gate/context 因子承担方向责任

- 状态: fixed
- 旧理解: 所有贡献度都可竞争 worst factor 并形成 downweight 建议。
- 当前口径: 只有 alpha 承担方向责任；gate 进入准入域，context/sizing 分别进入情境和仓位域。
- 影响面: `hours_to_fomc` 等事件因子、权重治理、复盘标签。
- 收口方式: review 中按 factor role 分域并保留兼容字段。
- 验证方式: reviewer/learning 回归测试。

### AWE 治理异常被调度日志吞没

- 状态: fixed
- 旧理解: AWE 调度只要捕获异常并继续运行即可，业务阻断和基础设施失败可以共用 warning。
- 当前口径: `FactorWeightChangeService` 明确返回 applied、blocked_by_admission/risk/replay 或 governance_error；扩张冻结时 AWE 整轮跳过，缺 V16 delegate 在 reservation/application 前收口；异常路径释放 reservation，AWE 使用结构化 Loguru 日志记录 stage、type、message 和 run_id。
- 影响面: AWE、实验准入、application/effect、RuntimeConfig mutation、运维审计。
- 收口方式: 统一结果契约，不增加权重写入旁路；冻结、预算和 replay 阻断属于正常治理结果。
- 验证方式: `tests/test_factor_weight_change_service.py`、`tests/test_evolution_closure_fixes.py`。

### 每日维护窗口被识别为行情故障

- 状态: fixed
- 旧理解: 计划交易时段内没有新 bar 一律按 stale data 升级 critical，并持续重复回补。
- 当前口径: 统一 market session 返回 open_pending_quote 或 broker_connected_market_data_stale、API/连接健康且无 broker error 时，按 RuntimeConfig 的75分钟上限进入 maintenance_wait；快照复用最新 spot quote，正常 open、到期或断连状态仍严格判 stale。
- 影响面: system health、data sync、告警和 readiness。
- 收口方式: 使用市场证据和限时宽限，不写死每日钟点；维护期抑制无效回补，首个新 bar 后自然恢复。
- 验证方式: `tests/test_market_maintenance_wait.py`、`tests/test_live_data_sync_job.py`。

### Prometheus 依赖未纳入生产安装

- 状态: fixed
- 旧理解: 指标 fallback 可长期视为等价生产后端。
- 当前口径: `prometheus-client` 是正式运行依赖；fallback 仅容灾，并在 backend readiness 中显示 degraded。
- 影响面: `/metrics`、监控告警和 readiness。
- 收口方式: requirements 固定依赖并公开 metrics backend 状态。
- 验证方式: `tests/test_metrics_endpoint.py`、`tests/test_backend_readiness_contract.py`。

## 5. 新旧债登记模板

复制下面模板新增条目：

```text
### 标题

- 状态: active | migrating | fixed | deprecated
- 旧理解:
- 当前口径:
- 影响面:
- 收口方式:
- 验证方式:
```

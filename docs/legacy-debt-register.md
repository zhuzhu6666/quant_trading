# Legacy Debt Register

> Status: active
> Last verified: 2026-07-19
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

### 兼容债务删除总门槛

- 单测、故障注入或代码完成都不等于运行观察完成。Safety v2 必须 shadow 至少一个完整持仓生命周期且零动作差异；无仓时必须完成 24 小时 shadow 与故障矩阵。当前尚未宣称达成该门槛。
- `live_safety_plane_v2_mode`、generation controller、execution outcome、governance coordinator 和 PG job queue 保持默认 off/false；只能按发布配置、受控重启和各自验收门分步切换。
- 旧 safety 尾部执行、loop globals/PID 猜测、V16 consume、direct overlay/Registry mutation、JobManager 本地重任务和 recursive frontend compat 只能在新路径经过一个稳定发布周期后删除。UI 旧字段还必须满足两个小程序版本或 30 天（取更长者）；Auth legacy 路径必须等全部客户端迁移完成。
- 不得回滚 emergency 严格对账、unknown execution 禁止猜测/重发、session unavailable 不归零、风险缩减不依赖 PG 以及 durable auth revocation 语义。新路径异常时应进入 `no_new_risk`/人工确认，不恢复假成功逻辑。

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

- 状态: `migrating`
- 旧理解: 搜索出来的因子容易被直接注册或进入主链。
- 当前口径: PostgreSQL `factor_lifecycle_state` 是 lifecycle 事实源，`factor_runtime_projection` 是加载确认事实；兼容 shadow promote 只提交 `PROMOTION_PREPARED`，ACTIVE 必须经 Coordinator/V16、稳定 artifact、fresh health 和 fresh loaded ack。RuntimeConfig/Registry 只作 committed 后投影。
- 影响面: discovery、shadow API、registry、runtime selection、AWE、readiness、Catalog、recovery。
- 收口方式: 新 `FactorLifecycleService` 已接管 shadow API、discovered 与 native builtin 的 prepare/activate/quarantine/retire；live warmup/hot reload 提供非投票式 loaded ack。builtin 激活现在分周期 enrollment/prepare/ack+health/activate，弱 builtin 进入 typed terminal QUARANTINED，代码 callable 保留但 admission 与权重归零；dual-record/enforce 不再执行 generic restore。旧 `CanaryDirector.retire()` 和 `_ensure_promoted_runtime_config()` 已无生产路径/已删除。coordinator-off 的 generic rollback/restore 只保留一版兼容，稳定发布后删除。
- 验证方式: `tests/test_factor_lifecycle_service.py` 覆盖 DSL/native 稳定 ID、代码 artifact、状态机、激活门、builtin Registry 保留、投影降级恢复和 route 无 direct registry mutation；`tests/test_factor_governance_recovery.py` 覆盖 staged builtin activation、typed quarantine 与终态不可复活。

### discovered 因子隐式 0.3 权重

- 状态: `fixed`
- 旧理解: Catalog 对未显式配置权重的 discovered factor 使用 `0.3`，可能让展示和治理把未准入候选误当成参与评分。
- 当前口径: 缺失或非法权重一律为 0，`explicit_weight=false`、`used_in_score=false`；ACTIVE 只能由 lifecycle activation 同时写入显式正权重。
- 影响面: Factor Catalog、runtime config、前端因子治理、ACTIVE admission。
- 收口方式: 删除默认 `0.3`，保留 additive `explicit_weight` 字段并以状态机测试锁定。
- 验证方式: `tests/test_factor_lifecycle_service.py::test_discovered_factor_without_explicit_weight_never_gets_implicit_default`。

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

### 历史 runtime overlay 缺少 committed mutation 权威绑定

- 状态: `quarantined`
- 旧理解: `runtime_config_overlay.mutation_id=''` 可以凭来源名或“当前行为看起来保守”在任意 coordinator mode 下继续恢复，悬空 mutation 也可以回退为普通 legacy overlay。
- 当前口径: 非空 mutation 必须对应 `status=committed`、`projection_status=current` 且 target/committed config hash 与 domain hash 完整绑定；空 mutation 只能凭 v9 `legacy_authority_json` 恢复，manifest 必须绑定当前 overlay hash、operator identity/review ID/time 和全部顶层 key，且中央 before/after 分类器逐 key 得出 `risk_tightening`。缺失、部分复核、hash 漂移或悬空 intent 一律激活 no-new-risk；已有仓位所需的只读 supervisor/收紧投影可以保留，扩张/未知字段不能据此生效。
- 影响面: backend/learning worker startup、长期进程 overlay refresh、demo 自治连续性、operator release preflight。
- 收口方式: 先 apply v9；在不重启 live 服务时读取精确 overlay hash/mutation ID。只有中央分类为 tightening 的历史 key 才能由 operator 调用 `RuntimeConfigOverlayService.review_legacy_quarantine()` 逐项回填；partial review 仍保持 blocked。任何非收紧 key 不得 grandfather，必须在 latch 下用 typed Coordinator mutation 重建或由 operator 显式清理 overlay，确认 committed projection 后再按 cause 身份释放 latch。
- 验证方式: `tests/test_runtime_overlay_authority.py`、`tests/test_governance_authority_boundaries.py`、`tests/test_factor_autonomy_hardening.py`。

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
- 当前口径: `data/experiments.db` 只有 structured canonical schema；未接线的 Evolution registry adapter 已移除。生产 ExperimentTracker/model 构造器只读校验，旧 blob 仅由显式 `scripts/experiments_schema_migrate.py --apply`（或兼容的 broader `db_doctor --repair`）原位迁移。
- 影响面: experiments API、周报、GP/模型实验记忆、db doctor。
- 收口方式: 统一到 `research.experiment_tracker.ExperimentTracker` 的数据 API，并由 `backend.core.db` 统一持有 schema validation/operator migration。
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
- 影响面: `backend/services/live_position_protection_cycle.py`、`backend/services/live_position_lifecycle.py`、持仓保护 trace、历史 close reason、测试兼容。
- 收口方式: legacy 计算仅作为 `ProtectionCandidate` 兼容适配器保留，统一由独立 protection cycle 按 timeout、entry repair、supervisor、trailing 固定优先级仲裁，再由 `PositionSupervisor` 和 `RiskPolicyService` 决定是否执行；它不再拥有独立执行权。历史 close reason 映射继续只用于复盘兼容。
- 验证方式: `tests/test_live_position_lifecycle.py` 覆盖候选生成，`tests/test_live_position_protection_cycle.py` 覆盖跨 stage 优先级与单仓 mutation authority，`tests/test_live_service_lifecycle.py::test_legacy_awe_trailing_records_protection_state_not_supervisor_cooldown` 覆盖执行边界。

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
- 当前口径: live 因子决策只能使用最新已闭合 bar，且已闭合 OHLC 在进入因子引擎前保持不可变；实时 spot 只服务执行参考价、滑点判断和持仓保护，不得覆盖 close/high/low。`classify_bar_freshness()` 按 timeframe 推导应有闭合 bar；`_ensure_live_decision_bars_fresh()` 会在因子计算前过滤未闭合 bar，缺 bar 时通过主 cTrader bridge 回补月库并重载。修复失败时只阻断 open_trade；同一根已闭合 bar 只允许推进一次 signal/open，重复 tick 只运行持仓观察/保护；即使 bar fresh，`RiskPolicyService` 也会在信号 age 超过 `max(180s, 1.5 * timeframe)` 时以 `decision_signal_age_stale` 阻断开仓，持仓监督和平仓链路继续运行。
- 影响面: `backend/services/live_data_sync_helpers.py`、`backend/services/live_service.py`、`backend/services/live_position_lifecycle.py`、`risk/policy_service.py`、同步健康、决策审计、学习标签解释。
- 收口方式: 新增 `decision_bar_freshness.v1` 运行态快照、`last_processed_decision_bar_ts` 和 `decision_freshness` 风控上下文；`StreamingFactorEngine` 拒绝重复/倒序 bar；live loop 对 spot 与 closed bar 只做无副作用偏差检查；`RiskPolicyService.evaluate("open_trade")` 对 `fresh=false` 返回 `decision_bar_stale`，对过期信号返回 `decision_signal_age_stale`，不改变 close/reduce/tighten 降风险动作。
- 验证方式: `tests/test_live_data_sync_helpers.py`、`tests/test_live_loop_shell.py::test_compare_spot_quote_to_latest_bar_never_mutates_closed_ohlc`、`tests/test_live_service_lifecycle.py::test_ensure_live_decision_bars_repairs_from_primary_bridge`、`tests/risk/test_policy_service.py::test_open_trade_blocks_stale_decision_bar_freshness`。

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
- 当前口径: 所有实际因子权重变更统一由 `FactorWeightChangeService` 编排；每次都包含有界经验先验、共享实验准入、RiskPolicy、runtime mutation 和后验观察。`dual_record/enforce` 下通过 Coordinator transaction writer 原子写 application/effect/reservation 并绑定 mutation；`off` 保留 legacy prepared 兼容恢复。
- 影响面: live AWE、因子降权/晋升、manual govern API、Evolution、学习效果账本。
- 收口方式: 三条生产路径已迁入统一业务用例；Coordinator 模式不再在 mutation 前落 prepared application/reservation，完整 batch 在 scope/global lock 内一次提交，故障时领域账本和 overlay/snapshot 一起回滚。旧 off 路径继续把 prepared 视为 active experiment 并支持 snapshot recovery。
- 验证方式: `tests/test_factor_weight_change_service.py`（atomic bind、fault rollback、double worker）、`tests/test_evolution_closure_fixes.py`。

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
- 当前口径: `FactorWeightChangeService` 明确返回 applied、blocked_by_admission/risk/replay 或 governance_error；非 Demo 扩张冻结时 AWE 整轮跳过，Demo nursery/autonomous 不受该冻结影响；缺 V16 delegate 在 reservation/application 前收口，异常路径释放 reservation，AWE 使用结构化 Loguru 日志记录 stage、type、message 和 run_id。
- 影响面: AWE、实验准入、application/effect、RuntimeConfig mutation、运维审计。
- 收口方式: 统一结果契约，不增加权重写入旁路；非 Demo 冻结、预算和 replay 阻断属于正常治理结果，Demo 仍保留 RiskPolicy、DecisionPolicy、V16 和后验回滚。
- 验证方式: `tests/test_factor_weight_change_service.py`、`tests/test_evolution_closure_fixes.py`。

### 扩张冻结未覆盖 Canary 证据阶段推进

- 状态: fixed
- 旧理解: Evolution 只写研究证据，因此其内部 `canary_state` 从 SHADOW 推进到 CANARY 阶段不属于扩张动作。
- 当前口径: Canary 阶段上升本身就是扩张性状态变化；非 Demo 冻结时仍刷新证据并允许回滚，但任何阶段上升都保持原 stage。Demo nursery/autonomous 的 effective freeze 为 false，允许阶段继续推进。
- 影响面: Evolution、Canary readiness、Factor Governance 和自动解冻判断。
- 收口方式: `_run_canary_evaluation()` 在调用 `CanaryDirector.promote()` 前统一调用 `autonomy_expansion_freeze_applies()`；非 Demo fail-closed，Demo 保持推进。
- 验证方式: `tests/test_evolution_closure_fixes.py::test_canary_stage_does_not_advance_while_expansion_is_frozen`、`tests/test_evolution_closure_fixes.py::test_demo_canary_advances_even_when_global_expansion_freeze_is_configured`。

### Supervisor tighten 使用过期的动作起始报价

- 状态: fixed
- 旧理解: 动作开始时完成一次 stop 合法性规划即可直接提交 broker。
- 当前口径: tighten 在 broker 提交前必须重新读取方向侧 bid/ask，并使用 RuntimeConfig 的 quote freshness、最小 stop distance、安全缓冲、最小 tighten delta 和 precision 重建计划；缺少方向侧报价或重建后不再合法时只审计 skip，不提交 amend。
- 影响面: Position Supervisor、cTrader amend、执行 trace 和 broker alignment readiness。
- 收口方式: supervisor action 执行器增加最终报价二次校验，不增加重试旁路或业务魔法阈值。
- 验证方式: `tests/test_live_supervision_actions.py::test_execute_supervisor_tighten_action_rechecks_quote_before_amend`。

### 每日维护窗口被识别为行情故障

- 状态: fixed
- 旧理解: 计划交易时段内没有新 bar 一律按 stale data 升级 critical，并持续重复回补。
- 当前口径: 统一 market session 返回 open_pending_quote 或 broker_connected_market_data_stale、API/连接健康且无 broker error 时，按 RuntimeConfig 的75分钟上限进入 maintenance_wait；快照复用最新 spot quote，正常 open、到期或断连状态仍严格判 stale；定时 data sync、live decision-bar 即时修复和 stale quote spot 重订阅共同遵守该窗口。
- 影响面: system health、data sync、live decision bar repair、spot subscription、告警和 readiness。
- 收口方式: 使用市场证据和限时宽限，不写死每日钟点；维护期抑制无效回补和重复订阅，首个新 bar 后自然恢复。
- 验证方式: `tests/test_market_maintenance_wait.py`、`tests/test_live_data_sync_job.py`、`tests/test_live_service_lifecycle.py::test_ensure_live_decision_bars_suppresses_repair_during_maintenance`。

### Prometheus 依赖未纳入生产安装

- 状态: fixed
- 旧理解: 指标 fallback 可长期视为等价生产后端。
- 当前口径: `prometheus-client` 是正式运行依赖；fallback 仅容灾，并在 backend readiness 中显示 degraded。
- 影响面: `/metrics`、监控告警和 readiness。
- 收口方式: requirements 固定依赖并公开 metrics backend 状态。
- 验证方式: `tests/test_metrics_endpoint.py`、`tests/test_backend_readiness_contract.py`。

### LightGBM 影子权限与真实影响之间没有统一晋级边界

- 状态: fixed
- 旧理解: 四个 LightGBM 要么永久 shadow，要么由各调用点自行解释分数，缺少统一的 PIT、晋级、影响审计和回滚口径。
- 当前口径: 训练统一发布 `pit.v2` 工件；`ModelInfluenceGovernanceService` 执行时间隔离、样本、基线提升和哈希门，V16 只下达一次性 `model_stage` 委派，RuntimeConfig 记录 shadow/demo_canary/demo_active/quarantined 阶段。
- 影响面: open candidate、position supervisor、factor governance、meta risk sizing、learning worker、readiness、runtime overlay。
- 收口方式: 模型只允许 veto/收紧/治理建议/额度收缩，不能放宽规则或获得 broker 权限；工件漂移、门槛回退和动作率超限自动 quarantine。
- 验证方式: `tests/test_model_influence.py`、四个 LightGBM 测试、live/risk/runtime contract 回归。

### LightGBM 训练混用版本、终局标签和规则重复标签

- 状态: fixed
- 旧理解: 只要样本数量够就可以把不同策略代际混训；持仓用最终平仓结果反标任意 trace，因子使用当期贡献预测自身结果，meta 直接重复预测规则 posture。
- 当前口径: open 使用稳定语义血缘隔离代际；position 使用固定 30 分钟前瞻 PnL 保全标签且同仓位降权，并修复 completed-bars 缺省值；factor 使用显式/可回溯的 bounded generation、独立 trade 数和同因子滚动历史预测下一结果；meta 使用固定 24 小时事件率、预测相对规则输出的残差，并以分布稳定性阻断规则输出塌缩。
- 影响面: 四套 LightGBM 的样本选择、标签、特征、artifact schema、promotion gate 和推理审计。
- 收口方式: 训练发布具体 `pit.v2.*` 子契约；因子展开行不得冒充独立样本，旧 unbounded generation 不与当前 bounded generation 混训，meta 的未来 posture 漂移超过 0.25 或规则单 posture 占比超过 0.90 时不得晋级；样本不足或不胜基线时保留 shadow/blocked。
- 验证方式: 四套 LightGBM 定向测试、真实 PostgreSQL 重训结果、`ModelInfluenceGovernanceService.evaluate_artifact()`。

### PostgreSQL state schema 仍由业务服务动态补表补列

- 状态: fixed
- 旧理解: `init_state_db()` 或任一业务 service 在首次调用时执行 `CREATE TABLE IF NOT EXISTS` / `ALTER TABLE`，即可长期替代正式 forward migration。
- 当前口径: PostgreSQL forward DDL 只允许进入 `migrations/state_pg/`，由 `scripts/state_schema_migrate.py --apply` 的专用 migration connection 在 advisory lock 下显式执行，并写 checksum 保护的 `state_schema_migration`。普通 backend/worker connection 和 cursor 在代码层禁止 schema write；service-local 旧 `CREATE IF NOT EXISTS` / `ALTER ADD COLUMN` 仅作为非写 catalog assertion，缺 table/column/index 时 fail-closed，不会补建。`init_all()` 与 experiments/model registry/shadow/canary/inference 构造器也不再启动期 ALTER 独立 `experiments.db`：canonical 文件只读校验完整表/列/索引，文件缺失时保持可选而不隐式创建；完整 SQLite schema 只由显式 `scripts/experiments_schema_migrate.py --apply` 写入（`db_doctor --repair` 为 broader 兼容入口），调用方显式提供的非 canonical 路径可作为隔离 fixture 自初始化。
- 影响面: backend/worker 启动顺序、RuntimeConfig overlay、学习治理表、数据库角色权限和滚动发布。
- 收口方式: v3 承接 durable jobs 和旧中央 compatibility 对象；v4 显式物化 worker/research audit、model influence、off-market audit 表与索引；v7 将 V16 委派授权时间从可变的 `updated_at` 分离为不可变 `authority_issued_at`；v8 物化 `runtime_kv`、factor `canary_state`、旧动态补列，并以新名字补齐 experience append-source 正确索引，保留 v4 同名旧索引而不做破坏性重建；v9 为 `runtime_config_overlay` 增加 `legacy_authority_json` 及 mutation/updated_at 查询索引，使历史空 mutation 行只能经 hash-bound operator review 隔离恢复。catalog validator 对索引校验 target table、unique、ordered keys 与 predicate presence，不再只看同名 regclass。高频 worker/model/readiness 路径已显式调用只读 catalog validator；其余 legacy ensure 仍由普通 connection/cursor guard 转为 assertion。`connect_state_store()` 不再隐式建 schema，旧 SQLite restore 命令只在已迁移 schema 上导入数据。数据库 runtime role 的 DDL 权限仍建议在稳定发布后由运维层撤销，作为第二道防线。
- 验证方式: `tests/test_state_schema_migrations.py`、`tests/test_state_store_schema_guard.py`、`tests/integration/test_postgres_state_store.py`、`scripts/state_schema_migrate.py --check`；生产必须先 apply/check/幂等复跑，再部署或重启当前最低版本 9 的 backend/worker，后续以代码常量为准。

### JobManager 自建事件循环线程承载重任务

- 状态: `migrating`
- 旧理解: backend API 可以在 `JobManager` 构造时隐式启动 daemon event-loop thread，并把 backtest/discovery/tuning/A-B 重任务留在 Web 进程内运行。
- 当前口径: `JobManager` 永不创建或拥有 event-loop thread；`pg_job_queue_v2_enabled=true` 时，`backtest/discover/tuning/ab_test/external_refresh/sync/factor_health/parameter_template_validation` 八类重任务只持久化到 PostgreSQL `jobs`，由独立 `scripts/job_worker.py` claim/heartbeat/complete。静态架构测试扫描所有生产 `.submit("kind")` 调用，要求任务种类与 worker handler 完全一致，避免静默退回进程内。flag 默认关闭期间，旧 API closure 仍可在 FastAPI 所属 loop 或调用线程内兼容执行，不产生孤儿 loop thread。
- 兼容执行所有权: flag-off 的同步 closure 使用 `JobManager` 懒加载、最多两个 worker 的显式 executor，不借用 FastAPI/asyncio default executor；lifespan stop 会先停止准入，再同步 cancel 并 join executor。重复或并发 shutdown 只有一个 join owner，持久队列路径不会启动该 executor。
- 影响面: backend 进程资源、重启恢复、任务取消/重试、研究并发和 systemd 发布顺序；不涉及 broker mutation 或 live safety authority。
- 收口方式: 先执行 v3 additive migration，部署 disabled worker，再打开静态 flag 并重启 backend/worker；按全局及 kind 限额观察 lease recovery。稳定发布后删除 flag-off 的本地重任务兼容执行，仅保留轻任务与历史查询。
- 验证方式: `tests/test_backend_jobs_manager.py`、`tests/test_persistent_job_handlers.py`、`tests/test_persistent_job_worker.py`、`tests/test_backend_runtime_lifecycle.py`、`tests/integration/test_postgres_job_queue.py`。

### backend readiness daemon 在线程退出时遗留原生库工作

- 状态: fixed
- 旧理解: readiness snapshot 可以为每次 stale 请求临时启动 daemon thread，仅靠全局 lock 单飞；进程退出时无需持有 thread handle 或等待 DuckDB/Pandas 工作完成。
- 当前口径: readiness refresh 按 state store 绑定 process-owned owner，记录 generation/thread，使用非 daemon 单飞 worker；`BackendRuntimeLifecycle.start()` 统一开启并调度，`stop()` 先拒绝新增 refresh，再 join 当前 worker。API 仍只读持久化快照且非阻塞。
- 影响面: backend lifespan、`/api/ops/backend-readiness`、DuckDB/native 资源析构、pytest/process exit。
- 收口方式: 删除 app 中无所有权的直接 thread 启动，把启动和 drain 都收进 lifecycle；timeout 后 owner 仍以非 daemon 保留所有权直到任务完成，不伪报 stopped。
- 验证方式: `tests/test_backend_readiness_snapshot.py` 单飞/drain 用例、`tests/test_backend_runtime_lifecycle.py`，以及 lifecycle + learning worker 组合进程退出码必须为 0。

### 紧急平仓把空列表和 order success 当作完成

- 状态: migrating
- 旧理解: `refresh_positions()` 返回空列表即可视为无仓，`close_position.success=true` 即可累计 closed。
- 当前口径: safety/startup/emergency 只接受带 reconcile ID/observed_at 的显式 fresh authoritative reconcile；push event 只进入非权威 event projection。position reconcile 仅证明 identity/volume/SL/TP，current price 与 PnL 分别要求 fresh spot 和独立 broker PnL RPC；未知组件不能按 entry price、账户差额或零值补齐。emergency 先持久化本地 no-new-risk latch 并与 open admission 线性化，只有 fresh post-reconcile 确认目标 position ID 消失才可报告 completed。
- 影响面: `backend.services.live_service`、cTrader reconcile contract、live emergency API、运行审计与 operator resume。
- 收口方式: 新 bridge 使用 immutable `PositionReconcileResult`；legacy `refresh_positions()` 只保留给非 safety 的值兼容调用，startup/recovery/emergency/safety 缺少 explicit `reconcile_positions()` 时一律 fail-closed。PG/审计失败进入 append-only safety outbox，不阻断风险缩减。
- 验证方式: `tests/test_live_emergency_safety.py`、`tests/test_live_service_lifecycle.py` emergency 定向用例。

### broker 延迟/未知回执被猜测为成功或可重发

- 状态: `migrating`
- 旧理解: market RPC timeout、未知 protobuf 或仓位差分不唯一时，可以用同方向最大 position ID / `positions_before[0]` 推测成交，或将本次视为失败后重发。
- 当前口径: `CTraderOrderResult.outcome` 只允许 `confirmed/rejected/unknown/simulated`，`success=true` 只属于 confirmed/simulated。V2 路径在 RPC 前依次 committed `broker_execution_intent.prepared/submitting`，以 UUID client ID、comment token 和 order/deal/position 差分唯一定位；无法唯一解析或 intent finalize 失败时必须 `unknown`、立即增加独立 `broker_execution_unknown` no-new-risk cause、禁止重发，重启先 recovery。同 position/action 未解决 intent 会阻断重复 close/amend RPC，但 PG/审计失败不得阻断首次风险缩减。通用 incident thaw 不得删除 unknown 证据；只有 fresh broker recovery/reconcile 明确得到 confirmed/rejected 后才能追加对应 resolution event 并按 intent 释放。
- 影响面: cTrader bridge、live open admission、restart recovery、entry protection、close/amend 幂等与 incident latch。
- 收口方式: `ctrader_execution_outcome_v2_enabled=false` 当前默认保留兼容路径；timeout/延迟回执/未知 protobuf/amend 未落地/重启防重复/intent 边界/PG-independent reduction 已进入 `execution_outcome_fault_matrix.v1` 的代码绑定持久证明，`execution_outcome_enable` 缺当前 passed attestation 时 fail-closed。仍需按阶段顺序完成受控 demo 观察后才随发布配置切换。稳定发布后才删除 PID 猜测和旧 result 兼容分支；unknown 禁止假成功/重发的语义不得回滚。
- 验证方式: `scripts/execution_outcome_fault_matrix.py`、`tests/test_execution_outcome_fault_matrix.py`、`tests/test_ctrader_execution_outcome.py`、`tests/test_broker_execution_intent.py`、`tests/test_live_execution_recovery_gate.py`。

### live loop 单例 globals 会在旧线程退出前释放所有权

- 状态: migrating
- 旧理解: stop 设置 event 后立即清空 `_loop_thread` 即可让新 start 成功；market closed、bar/circuit/PG 失败可在持仓保护前提前 return，缺 session cache 时可以重置为零。
- 当前口径: generation v2 开关无论开启还是关闭，draining 都一直保留 thread ownership 到真实 exit，stop flag 先阻断后续 open admission、再等待已准入 broker RPC，replacement start 在 owned thread 存活期间固定拒绝；每轮显式 broker snapshot 和 safety 先于 session/circuit/bar/alpha，startup barrier 未完成、safety heartbeat 缺失/过期、unknown execution 或 session `unavailable/degraded_cache` 都阻断新增风险。session 恢复以 deals 为主并排除 broker 仍开放 position。Safety shadow 已不再把 candidate 自己与自己比较：V2 planner 与独立 legacy read-only preview 都覆盖 timeout、entry repair、supervisor close/reduce/tighten 和 trailing，先按稳定 SHA-256 指纹与候选计数比较；重复指纹或同一 position 的多 mutation candidate 不能进入 enforce。shadow 再执行 legacy authoritative cycle 并核对实际仲裁；enforce 只有纯比较匹配后才逐 V2 candidate 执行。不独立、重复/冲突、异常或 mismatch 会在 broker mutation 前持久化 forced-shadow/no-new-risk cause，并让本轮 legacy authoritative protection exactly once、V2 zero；forced shadow 跨后续周期及 generation 重建保持，避免半轮或跨轮混合 mutation authority。
- 影响面: live start/stop/status、backend shutdown、position protection、account/position freshness、session circuit、AutoRecovery 和前端 loop readiness。
- 收口方式: `live_generation_controller_v2_enabled=false` 继续保留旧 generation，`live_safety_plane_v2_mode=shadow` 已开始真实观察；“stop 立即清 globals”已从默认路径删除。独立 shadow comparison 的代码和测试已具备，但仍必须实际 shadow 一个完整持仓生命周期，或在无仓时完成 24 小时观察与故障矩阵，才能随发布/重启切换 generation 与 safety enforce。稳定发布后删除其余 loop globals、并发 refresh worker 和旧 safety 尾部执行。
- 验证方式: `tests/test_live_generation_integration.py`、`tests/test_live_loop_controller.py`、`tests/test_live_safety_plane.py`、`tests/test_live_service_lifecycle.py`。

### live_service 同时承载 reconcile、safety、startup 与 emergency 领域实现

- 状态: `migrating`
- 旧理解: 为复用进程内 globals，可以继续把 explicit reconcile、safety cycle、startup barrier 和 emergency 状态机直接写进 `live_service.py`。
- 当前口径: `backend.services.live_reconciliation` 只负责 fresh broker contract，`backend.services.live_loop_v2` 负责 safety/startup 顺序，`backend.services.live_emergency` 负责严格紧急平仓；三者均不依赖 PostgreSQL。`live_service` 仅保留兼容状态发布、process wiring 和 callback 注入。
- 影响面: live loop façade、broker reconcile、position protection、generation barrier、emergency API；不改变 feature flag 默认值或 broker mutation 串行所有权。
- 收口方式: 本轮已移出上述新增实现并保留薄兼容入口；session restore 的 deals-first 重建及 `available/degraded_cache/unavailable` 选择由 `session_restore.resolve_session_restore()` 纯函数拥有；旧 protection 仲裁已迁入 `live_position_protection_cycle`；recovery CRUD/replay/retirement 已迁入 `live_recovery_position_store`/`live_recovery_close`；close 后 fail-closed 顺序与 attribution/audit/learning/cleanup 分别迁入 `live_closed_position_cycle`/`live_closed_position_processing`；emergency fallback 已迁入 `live_execution_recovery`。`live_service` 对这些边界只注入 callback/connection/static flag runtime。后续继续迁出其余 lifecycle/execution wiring；稳定发布后再删除 legacy compatibility authority 与旧 globals。
- 验证方式: façade 架构测试强制无 recovery/post-close 决策文本且 wrapper 无循环/异常；`tests/test_live_recovery_position_store.py`、`tests/test_live_recovery_close.py`、`tests/test_live_closed_position_cycle.py`、`tests/test_live_closed_position_processing.py` 分别覆盖持久化、projection-before-release/fresh absence、session rebuild 和 attribution/ledger/learning/cleanup；其余由 lifecycle/generation/emergency 回归。

### 治理 mutation 缺少跨账本事务提交权

- 状态: migrating
- 旧理解: V16 command 可以在 overlay 写入前 consume，overlay/snapshot 提交后直接发布内存；application/effect、reservation 和 Registry 可由各领域服务分别补写，局部成功即可解释为已应用。
- 当前口径: `GovernanceMutationCoordinator` 是治理提交权的唯一目标边界。它先 durable reserve，再在同一 PostgreSQL 事务内重验 before、写 prepared intent、legacy overlay/snapshot、领域事实并 finalize V16；commit 后才发布 RuntimeConfig，publish 失败记 `projection_status=degraded` 并从 committed snapshot 重放。风险分类只由 before/after 推导，调用方自报 rollback/risk_reduction 或伪装 action 名不能免闸门。ParameterTemplate、position supervisor、model influence、factor lifecycle、incident control、live autonomy、autonomy freeze 与 operator expansion pause 均已进入 typed plan；旧 `/api/shadow/promote` 只写 `PROMOTION_PREPARED`。
- 影响面: RuntimeConfig mutation、V16、factor/parameter/supervisor/model/autonomy/incident 治理、application/effect/reservation、本地 safety latch/outbox 与进程投影。
- 收口方式: release-time `governance_mutation_coordinator_v2_mode=off|dual_record|enforce` 已从首阶段 `off` 推进到 `dual_record`；dual_record 同时维护旧 overlay/snapshot 与新 intent，不做一次性切换，但启动恢复只允许 committed projection 授权 live，未绑定的旧 registry/application 仅保留供重验。V16 改为 `available -> claimed -> finalized`，只有 Coordinator 事务 finalize 增加 apply_count 并绑定 mutation/config/domain hash；不可变 `authority_issued_at` 决定授权年龄，claim/release/recovery 不得用 operational `updated_at` 续期。backend 和 learning worker 启动时收口过期 claim 和停滞 intent，但不恢复成新授权。旧 consume 与 typed plan 内明确标注的 `off` 兼容分支保留一版。incident/revoke 收紧在 PG 前先激活本地 latch，PG、RiskPolicy 或审计失败写 outbox 且不依赖治理投影完成；thaw/unlock/unfreeze/operator resume 均视为扩张，要求最近 step-up（operator risk unlock）并 fail-closed 等待 V16。稳定一版后才能删除兼容分支。
- 验证方式: `tests/test_governance_mutation_coordinator.py`、`tests/test_governance_control_plans.py`、`tests/test_governance_runtime_controls.py`、`tests/test_v16_command_finalize.py`、`tests/test_state_schema_migrations.py`。

### learning worker 启动失败被降级且 mutation 与观察能力耦合

- 状态: fixed
- 旧理解: PostgreSQL、schema、YAML、overlay 或 recovery 启动失败可以只写 warning 后继续注册治理任务；任一 mutation 依赖故障只能让整个 worker 共同降级，readiness 无法比较 backend/worker 配置事实。
- 当前口径: 五类关键启动失败全部向上抛出并令进程非零退出；`runtime_kv[learning_worker.capability.v2]` 发布 boot/config/overlay/recovery 与三类 capability。连续三次 mutation 依赖失败只打开锁存 mutation circuit，scheduled sample/backfill/supervisor observation 和独立研究任务继续。
- 影响面: `quant-learning-worker.service`、factor governance/nursery/evolution 调度、RuntimeConfig 投影与 backend readiness。
- 收口方式: mutation job 经 `guarded_mutation_job` 统一计数；worker 每 30 秒刷新 committed config/overlay hash 和能力投影，backend 超过 75 秒或 hash 分歧即把 autonomous mutation 判为 unavailable。
- 验证方式: `tests/test_learning_worker_capability.py`、`tests/test_readiness_dimensions_v2.py`。

### frontend readiness 被控制和发布流程复用为授权

- 状态: fixed
- 旧理解: `ready_for_frontend=true` 且顶层 blockers 为空可以授权 live autonomy unlock 或 release checklist。
- 当前口径: readiness v2 分离 frontend、live execution、live alpha、autonomous mutation、release 五个维度；frontend 只表示展示契约可用，绝不授权 control/release。live unlock 同时要求 live-alpha 与 autonomous-mutation，release checklist 只读取 release 维度。
- 影响面: backend readiness、live autonomy、release ledger、Web 运维展示。
- 收口方式: 每个维度保留独立 blockers 和 authorization boundary；v1 历史 payload 只从 live facts/顶层 blockers 保守推导，不能回退读取 frontend bool。
- 验证方式: `tests/test_readiness_dimensions_v2.py`、`tests/test_live_autonomy.py`、`tests/test_v15_runtime_platform_phase0.py`。

### live 直接消费 approved policy suggestion

- 状态: fixed
- 旧理解: entry cluster、entry quality、event window 风控可把 `approved` 与 `applied` 同时当作 live active control；持仓监督在 expansion freeze 时还会从 live loop 读取最新 approved supervisor template 并生成 `canary_shadow`。
- 当前口径: live 查询固定只读取 `applied`，绝不读取 `approved/auto_approved`。Evolution 的 factor learning summary 可以观察这些审批状态，但生成权重 bias 时必须复用同一 committed-policy 边界；Supervisor 候选回放也已迁到 learning worker 的 closed-position observation 路径，固定为 `learning_shadow/observation_only/recovered`，不调用 broker，live loop 不再 import 或查询 approved candidate。Coordinator enforce 只接受绑定 committed mutation 的 applied 控制；off/dual 仅兼容保留无 mutation id 的已应用保守控制并标记 `legacy_quarantined`，混合的 `downweight/boost_small` action 集中仅 legacy downweight 可执行，悬空/未提交 mutation 一律拒绝。
- 影响面: live 开仓风险上下文、持仓监督、Evolution 权重 bias、learning repair readiness/auto-unfreeze、policy suggestion 生命周期、治理切换与回滚。
- 收口方式: `backend.services.live_committed_policy` 是可执行控制的统一只读过滤边界；`materialize_position_supervisor_candidate_observations()` 是 approved supervisor 候选唯一的非执行观察入口。readiness 只接受与当前 suggestion ID 精确绑定的 learning observation，旧 live `canary_shadow` fail-closed。旧 applied 控制完成重验和显式 committed mutation 回填后，随 coordinator enforce 删除 legacy 兼容。
- 验证方式: `tests/test_live_committed_policy.py`、`tests/test_evolution_closure_fixes.py` 与 `tests/test_live_policy_authority_boundary.py`；approved/auto-approved 在 dual/enforce 均为零执行影响，Supervisor approved candidate 只生成非执行 learning observation，只有 committed applied 或兼容期 legacy applied tightening 可进入 live bias。

### legacy indicator sweep 被当作部署或治理证据

- 状态: fixed
- 旧理解: `backend.services.backtest_runner` 的参数 sweep 只要返回收益指标或进入 parameter-template offline report，就可以生成 pending release candidate，调用方也可以用 `governance_eligible=true` 覆盖其可信度。
- 当前口径: 该引擎固定为 `engine=legacy_indicator_sweep`、`evidence_class=diagnostic_only`、`live_parity=false`、`governance_eligible=false`、`deployable_candidate=false`。CLI、runner、service、job/list/report API 都在最后一步强制覆盖标签；统一 research evidence policy、governance eligibility、参数模板 approve/deploy 和模型 promotion gate 均 fail-closed。
- 影响面: backtest job/API/report、parameter-template offline validation、model promotion、governance sample eligibility。
- 收口方式: legacy 输出继续用于参数敏感性诊断，但携带它的新 release candidate 只保存为 `diagnostic_only`，不能 approve/deploy；任何可执行入口必须调用 `backend.services.research_evidence`，不得相信调用方自报字段。
- 验证方式: `tests/test_research_parity_boundaries.py` 的 contract spoof、report、governance zero、parameter deploy 和 model gate 用例。

### 参数模板缺失 research metadata 可绕过 approve/deploy

- 状态: fixed
- 旧理解: `parameter_template_release_candidate.validation_summary_json` 没有 `research_evidence` 时，review/deploy 可把“没有证据”解释为无需校验；历史手工 approved 行因此能直接切换模板。
- 当前口径: 新候选无条件由 `backend.services.research_evidence` 重验，只有完整 executable parity contract 才进入 `pending_review`。缺 metadata 的新候选标记 `require_revalidation`；没有 `research_evidence_policy.v1` 记录标记的历史 pending/approved 行以 `legacy_quarantined` 兼容读取，首次执行尝试会持久化隔离标记并拒绝。调用方缓存的 `research_evidence_verdict.allowed=true` 不构成证据。
- 影响面: parameter-template candidate 注册、review、deploy、历史候选展示与模板 rollback。
- 收口方式: 历史候选必须以新的 parity artifact 重新注册并重新 review，不能直接 deploy；历史 deployed 控制保持当前行为和 rollback 通道，回滚不依赖 research evidence。
- 验证方式: `tests/test_research_parity_boundaries.py` 的 missing metadata、legacy quarantine、valid parity contract 用例，以及 parameter-template release/rollback 回归测试。

### parity replay 尚未具备完整 live lifecycle 等价性

- 状态: migrating
- 旧理解: 复用 FactorFrame/normalizer/compositor 并按历史 OHLC 计算成本，就足以把离线回放标成 live parity。
- 当前口径: `backend.services.parity_replay` 已建立 closed-bar → next-bar bid/ask、成本、config/data/code/factor-artifact manifest hash binding 和逐组件 exact/modeled contract。factor/selector/normalizer/compositor、RiskPolicy、position path metrics、safety candidate arbitration、supervisor、trailing 与 protection plan 已复用 live 纯决策原语；历史时间通过显式 `evaluated_at_ts` 进入共享保护计划，避免 wall-clock 污染。首次运行只发现 hash，只有显式提供并匹配 config/data/code/artifact 四个 expected hash 才算 binding verified；月库缺失/命名非法/部分读取错误、代码 binding 文件缺失、所选 generated/discovered factor 缺规范 ID 或存在与规范 AST 不一致的 definition fingerprint、稳定 artifact、ACTIVE committed lifecycle、显式 enabled 或正权重都会成为 blocker，selector 的 selected/excluded/reason 也绑定进 artifact。复用函数不代表端到端等价：历史 factor projection ack/health/Registry generation、broker receipt/reconcile/partial fill、tick 内触发顺序、5 秒 safety cadence 与 AWE 历史、权威 account/session/runtime context、真实 commission/swap 和 amend projection ack 仍不可由月度 bars 验证；当前月库也缺完整原生 bid/ask，因此强制 `diagnostic_only` 且治理数量为零。
- 影响面: 月度 bars schema、replay artifact、RiskPolicy/supervisor context、研究 API、未来 backtest/live parity 门禁。
- 收口方式: 先保留只读 `/api/ops/replay/parity-run`、逐文件/逐因子 artifact hash 和 exactness blocker；后续必须接入 PIT runtime factor projection/health/Registry generation、原生 bid/ask PIT、broker intent/order/deal/position/reconcile 事实、tick 级 safety 顺序、真实 account/session/AWE/cost/projection-ack 上下文，并由独立 certification 路径重验后，才可考虑 live-parity evidence。runner 本身永不自授权。
- 验证方式: `tests/test_research_parity_boundaries.py` 的时间因果、bid/ask 成本、四类 expected hash precondition/mismatch、factor artifact identity、live safety timeout/partial/trailing/protection primitive、确定性 quote age、missing quote 和 diagnostic-only 用例。

### matured 或 supervised-training 样本被等同为可执行治理证据

- 状态: fixed
- 旧理解: `label_status=matured` 或 evidence contract 允许 supervised training 即可按 raw sample count 进入 entry cluster/event window/entry quality 治理，历史缺失资格字段也可被兼容放行。
- 当前口径: 所有 sample upsert、maturation 和 evidence repair/backfill 统一调用 `governance_eligibility.v1`，持久化 contamination、eligible、effective weight、version、fingerprint 和 exclusion reason。full 权重 1，verified recovered 上限 0.5，其余为 0；训练资格不等于治理资格。
- 影响面: `autonomous_learning_sample`、`experience_pattern_stats`、样本型 `policy_suggestion`、`RuleEvolutionGovernor`。
- 收口方式: materializer 只消费当前 version、非空 fingerprint 且 weight > 0 的行；stats 保存 effective sample count 与 weighted win/bad-loss/reward；Governor 要求 stats/suggestion version+fingerprint 一致，否则拒绝 mutation，但保留 observation/research 数据。
- 验证方式: `tests/test_governance_eligibility_weighting.py`、`tests/test_autonomous_learning.py`、`tests/research/test_rule_evolution_governor.py`、`tests/integration/test_postgres_state_store.py`。

### API 零值/旧 status 被前端误判为健康事实

- 状态: migrating
- 旧理解: 页面可递归搜索任意 `status/ok/items`，请求失败时以零值补齐账户、持仓和风险，并继续显示绿色。
- 当前口径: 后端 additive 输出 `fact.v1`，新客户端只按 `docs/api-fact-contract.md` 的 endpoint-specific contract 和 component 读取；`api_fact_views`、`learning_fact_views` 和 `ops_governance_fact_views` 分别使用 broker/runtime 观测、显式持久化时间、durable commit/mutation 证据。缺 `_fact`、unknown、stale、error 均不得显示绿色或授权 start/unlock，最后 known 值可带时间保留。stop/emergency 始终可触发。
- 影响面: account/positions/loop/risk/readiness/learning/model/ops/governance API、Web FactBoundary、小程序 reducer/WS merge。
- 收口方式: 先后端、再 Web、再小程序迁移；旧字段保留两个小程序版本或 30 天取更长者，稳定后删除 recursive compat。
- 验证方式: `tests/test_api_fact_views.py`、`tests/test_learning_fact_views.py`、`tests/test_ops_governance_fact_views.py`、`tests/test_fact_envelope.py`、`web_frontend/src/tests/fact-behavior.test.mjs`、`tests/miniprogram_store_reducer.test.mjs`。

### SHA-256 登录与 URL JWT 兼容路径

- 状态: migrating
- 旧理解: 密码固定存 SHA-256，access token 长期使用，WebSocket 可把 JWT 放在 URL，stop/emergency 与普通接口共用会话依赖。
- 当前口径: 默认 Argon2id、15 分钟 access、7 天旋转 refresh session 和 30 秒单次 WS ticket；高风险扩张要求最近 5 分钟 step-up。`/api/auth/step-up` 以当前 active 持久会话和密码为前提，事务提交 `auth_time` 后才签发新 access；普通 refresh 继承而不刷新该时间。Web 的 start/unlock 收到 `step_up_required` 后原位要求密码并自动重试，stop/emergency 不增加密码障碍。普通 access 绑定 PostgreSQL active session；logout 撤销整个 refresh family，并在 PG 提交前 fsync 本地 append-only session/family 撤销投影，因此 backend 重启后旧 access/risk-reduction token 不会复活。stop/emergency 使用本地可验证的 risk-reduction scope和该撤销投影，PG 故障不阻断风险缩减。
- 影响面: backend auth、Web/小程序会话、WS、live start/stop/emergency、operator thaw/unlock。
- 收口方式: 三个 legacy 开关只在客户端迁移窗口显式开启；客户端全部迁移后关闭并删除 SHA-256、legacy access 与 URL JWT 解析。持久 logout 回归必须同时覆盖进程内缓存清空、旋转 family 旧 token 和 PostgreSQL unavailable 的 risk-reduction 路径；step-up 回归覆盖 session/family 绑定、提交失败 fail-closed、refresh 继承和 Web 仅对 start/unlock 提示密码。
- 验证方式: `tests/test_auth_v2.py`、`tests/test_backend_live_api.py`、`web_frontend/src/tests/fact-auth.test.mjs`。

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

# quant_trading canonical_v2 数据逻辑重建计划

> Status: active
> Owner: state-data architecture / backend learning
> Scope: PostgreSQL state_v1 的事实、payload、lineage、记忆投影和训练数据重建
> Last updated: 2026-08-16

## 1. 目标和边界

目标：

```text
唯一 canonical facts
  -> content-addressed payload
  -> 小型事件和 lineage 引用
  -> 可重建 memory / catalog / readiness projection
  -> 可复现 training dataset manifest
```

已确定的决策：

- 在现有 PostgreSQL 数据库内创建隔离 schema `canonical_v2`，不新增数据库、服务、scheduler、向量库或外部 memory source。
- 使用稳定类型列 + 单份 hash payload；不采用全 JSON 或完全关系化。
- 第一阶段不接入 Graphiti、Mem0、Letta、pgvector；后续外部记忆只能作为 projection。
- 先完成一条 `decision -> execution -> position -> review -> sample -> dataset -> artifact` 垂直链路，再分批回填历史。
- 旧 `state_v1` 在切换前后只读；至少覆盖完整交易生命周期、治理回放、training-only 和 projection rebuild 后再讨论退役。
- 不允许新旧系统双写；不删除历史事实，不执行 `VACUUM FULL`，不清理 outbox。
- 保留工作区现有全部未提交修改，禁止 reset、checkout、覆盖文件或 `git add -A`。

## 0. 2026-08-15 首批实施状态

已完成并验证：

- 在工作区外生成 PostgreSQL custom-format 逻辑备份，并用 `pg_restore --list` 验证可读；
- 以 schema-only migration 16 创建 `canonical_v2`，现有 `state_v1` 未回填、未删除、未重写；
- 建立隔离的 payload、event、relation、state version、training sample、dataset manifest、projection run 和 legacy mapping writer 合同；
- 通过 SQLite contract tests 和真实 PostgreSQL 事务 smoke test；smoke 数据已回滚，canonical_v2 当前为空；
- 完成只读 vertical shadow 和 legacy backfill planner；两者都未写入 `canonical_v2`；
- 修复 `DecisionLedger.log_position_supervisor_trace` 的写入边界：递归的 `latest_supervisor/latest_protection` 不再进入 trace 热字段，也不再原样进入 archive；无 archive 能力的旧兼容表同样只写 bounded projection；
- 将本计划加入 `docs/README.md`，并在 `docs/legacy-debt-register.md` 登记 active migration。

本批明确未执行：历史 backfill apply、生产 writer 切换、新旧双写、vertical lifecycle 运行、projection rebuild、payload compact apply、历史删除、`VACUUM FULL`、服务或 scheduler 启动。position-quality reader 的 `limit=4000, horizon=30` 只读 dry-run 已通过：一条 `position_id=280531397` 的 trace 窗口估算超过 128 MiB，已在 SQL 预算阶段排除；其余安全 lineage 完成读取，未把该窗口的 payload 送入 Python。第一次 training-only 调用因监控器误把只读 server-side cursor 的 `FETCH` 识别为写事务而保护性终止；修正恢复规则后，同一 window 的唯一重试已成功完成，未发生 OOM、promotion、governance 或 model registry 写入。上述动作必须按后续阶段和用户确认门执行，不能把“schema 已创建”解释为 canonical_v2 已成为生产 authority。

## 0.1 2026-08-16 归档读取收敛状态

已完成并验证：

- 将 supervisor/review 的直接读取收敛到 verified archive loader：learning/risk API、autonomous learning、live risk metrics、governor、V16 planning、phase-C、DuckDB reconcile、entry-context backfill、canonical vertical shadow 等路径均在数据库连接仍打开时恢复完整 payload；纯 `ExperienceBuilder` 只接受调用方传入的 payload，不作为数据库事实读取者。
- position-quality reader 现在也按同一 archive authority 恢复 review：若存在 archive reference，预算预估使用 `review_raw_bytes`，无效 metadata 直接触发 fail-closed；只有通过预算后才恢复 JSON，旧 inline 行和 SQLite 夹具仍兼容。
- supervisor/review consumer registry 当前为 `31 migrated / 0 pending`，但这只是代码覆盖门，不代表已执行 compact。
- governor 的 supervisor review SQL 不再依赖已被 bounded projection 截断的 `review_json LIKE`，改为有上限的候选扫描后用完整 archive 判定；V16 evidence review 扫描也改为有界读取，避免无界 `fetchall()`。
- backfill 写回 review 时复用 archive hash、raw SHA-256 和 byte metadata；没有 archive 能力的 SQLite 测试夹具保留 legacy inline fallback。
- 隔离 SQLite contract 新增完整 vertical lineage 验证：六类 payload 只各存一份，六个 event 通过五条 relation 串联，sample 和 dataset member 只保存引用；不存在跨表完整 JSON 写入。
- 新增只读 [`scripts/canonical_v2_consistency.py`](../../scripts/canonical_v2_consistency.py)，检查 canonical_v2 九张表、所有引用 orphan 和 payload restore/hash；当前真实 PostgreSQL 结果为 `ok=true`、九张表全 `0` 行、orphan 全 `0`、payload integrity `complete=true`、`writes_performed=false`。
- 针对性验证通过：governor 45、autonomous/live/API 77、V16/canonical shadow/rule learning 52、archive/dedupe/position-quality/offmarket 57（合计 231 条通过）；`tests/test_state_schema_migrations.py` 中缺少工作区既有 `.github/workflows/quality-gates.yml` 的单项失败未作为本批回归归因。
- 最新收口复跑：`tests/test_position_quality_lightgbm.py`、`test_offmarket_high_load.py`、canonical/backfill/shadow/archive/dedupe/writer-bound 共 `47 passed`；migration 定向测试 `25 passed, 1 deselected`；V16/autonomous/factor hardening 共 `89 passed`。`git diff --check`、目标文件 `py_compile` 和 schema check 均通过。

仍未执行：compact apply、历史 backfill、canonical vertical 写入、projection rebuild、服务或 scheduler 启动、删除历史和 `VACUUM FULL`。training-only 已在同一 window 完成一次成功重试；该次监控采样只有 1 个周期，资源峰值只能按“已观测值”记录，不能声称获得完整精确 peak RSS。payload apply 仍需独立批准。

本批只读证据：`./.venv/bin/python scripts/canonical_v2_vertical_shadow.py --limit 50` 返回 `complete_count=50`、`incomplete_count=0`、`selected_sample_rows=91`、`semantic_duplicate_candidate_count=0`、`writes_performed=false`；新 manifest [`data/state_payload_compact_maintenance_20260816_supervisor_review_dry_run.json`](../../data/state_payload_compact_maintenance_20260816_supervisor_review_dry_run.json) 的 SHA-256 为 `0ee48f91d6c6d0764651562a54c3f7c6413863136ac5b9c0921d7faa90a7eb91`，确认 `all_migrated=true`、`position_supervisor_trace=47768`、`trade_outcome_review=721`、原始 JSON `434169813` bytes、预计 gzip archive `76322219` bytes、预计临时空间 `510492032` bytes、exact duplicate payload `0`。

同日重新运行 [`scripts/canonical_v2_legacy_backfill.py`](../../scripts/canonical_v2_legacy_backfill.py) 的完整 vertical dry-run（`--batch-size 256 --max-rows-per-table 100000`）：扫描 decision `16791`、order `1357`、position `2956`、review `721`、learning sample `58632`；sample 的 `source_table/source_id` 通过数据库 `EXISTS` 校验，所有映射均为可解析引用，`quarantine_rows=0`，`writes_performed=false`，mapping digest 为 `ffe1194e39e2a1b80355c700df42d884ce5fd1c0dd15a8ffb62bcc8d94161257`。sample JSON 仅通过数据库 `OCTET_LENGTH` 统计为 `768038702` bytes，未载入 Python；`2772` 个 source 被两个不同 sample projection 引用（decision `2152`、review `620`，每个 source 最多 2 个 sample），暂按 projection reuse 处理，不按 source ID 删除或合并事实。canonical_v2 九张表仍全部为 `0` 行。

### 0.2 2026-08-16 fresh compact dry-run

在训练完成后重新执行全量只读 dry-run：

```text
./.venv/bin/python scripts/state_payload_compact.py --dry-run --targets all --batch-size 256 --chunk-size 4096 --manifest /var/tmp/state_payload_compact_canonical_v2_fresh_20260816.json
```

manifest [`/var/tmp/state_payload_compact_canonical_v2_fresh_20260816.json`](</var/tmp/state_payload_compact_canonical_v2_fresh_20260816.json>) 的 SHA-256 为 `49a6aa31d9591cfc42ea0549cc592c0db335dd2b289382a485851b0ae0dc0c50`。结果为 `ok=true`、`read_only=true`、active writer `0`、pending mutation `0`；未执行 apply、UPDATE、DELETE、VACUUM 或物理重写。

域摘要如下（payload 字节是逻辑估算，不是物理磁盘回收承诺）：

| 域 | 行数 | distinct payload | 重复引用行 | 原始逻辑 bytes | 可共享 payload bytes | 逻辑重复 bytes |
|---|---:|---:|---:|---:|---:|---:|
| `runtime_config_snapshot` | 14,597 | 3,582 | 11,015 | 5,414,094,369 | 1,975,132,112 | 3,438,962,257 |
| `brain_action_plan_eval` | 141,514 | 3,202 | 138,312 | 10,614,964,563 | 246,196,508 | 10,368,768,055 |
| `evolution_decision` | 44,734 | 35,787 | 8,947 | 10,830,956,243 | 6,959,236,527 | 3,871,719,716 |

三域合计原始逻辑 payload `26,860,015,175` bytes；预计共享压缩 archive `76,322,219` bytes；预计临时空间 `12,716,065,737` bytes；逻辑重复下界 `17,679,450,028` bytes。监督/review 范围为 `position_supervisor_trace=47,768`、`trade_outcome_review=721`，原始 JSON `434,169,813` bytes，预计 archive `76,322,219` bytes；exact raw payload duplicate 为 `0`，因此这部分主要是 archive/递归膨胀问题，不是可以按 hash 删除的事件重复。

该 dry-run 同时暴露两个不能静默合并的 lineage 问题：`audit_double_write` 为 `api_audit_rows=14,471`、`canonical_events=30,263`、`linked=55`、`conflicts=5,927`、`unmatched=8,489`。这些数字只能作为待分类的旧审计关联差异，不能直接当作重复数据清理，也不能作为 canonical event 合并依据。`position_supervisor_trace` 有 `93` 个 oversized JSON，最大递归深度 `491`；`trade_outcome_review` 有 `1` 个 oversized JSON，最大递归深度 `494`。apply 前仍必须单独确认 lineage 分类、archive restore/hash 验证和资源余量。

## 2. canonical_v2 数据模型

### 2.1 payload_blob

大 JSON 或结构化 payload 只保存一次：

```text
payload_hash, payload_kind, schema_version
canonical_bytes, codec, raw_sha256, raw_bytes, compressed_bytes, created_at
```

hash 绑定 `payload_kind`、`schema_version` 和稳定 JSON 字节。相同 payload 只插入一次；不同真实事件可以共享同一 payload。payload 追加写入，不能原地覆盖。

### 2.2 event

事件表只保存身份、时间、来源和引用，不保存完整大 JSON：

```text
event_id, event_type, entity_type, entity_id
observed_at, recorded_at
producer, producer_version, schema_version
correlation_id, causation_id, parent_event_id
idempotency_key, payload_hash, status
```

第一批事件类型：

```text
market_observation, broker_execution, position_transition,
risk_decision, factor_observation,
governance_proposal, governance_command, governance_effect,
trade_review, label_observation, training_run
```

新 schema 使用 `TIMESTAMPTZ`；旧 epoch 只在 legacy adapter 转换。canonical event 不允许覆盖历史。相同 `(producer, idempotency_key)` 重试返回已有事件；同 key 不同 payload fail closed。相同 payload 的不同真实事件不能被错误合并。

### 2.3 event_relation

保存事件因果和 lineage：

```text
from_event_id, to_event_id, relation_type, created_at
```

关系类型：`caused_by`、`derived_from`、`reviews`、`labels`、`uses_config`、`uses_factor_state`、`produced_sample`、`included_in_dataset`、`produced_artifact`、`governed_by`。

唯一键为 `(from_event_id, to_event_id, relation_type)`。memory、governance 和 training 只通过 relation 或 source ID 连接，禁止复制来源 JSON。

### 2.4 state_version

配置、因子状态和治理目标保存不可变版本：

```text
state_version_id, entity_type, entity_id, version
valid_from, valid_to, source_event_id, payload_hash, created_at
```

`before`、`target`、`rollback` 不再分别保存完整 JSON。rollback 等于 before 时复用同一版本或 hash。当前状态由 typed projection 提供，历史版本不可修改。

### 2.5 training_sample

训练样本是可重建 projection，不是原始事实源：

```text
sample_id, sample_type, source_event_ids
feature_hash, feature_schema_hash, label_hash, trace_hash
evidence_contract, config_version, config_hash
horizon_minutes, target_source, sample_status, created_at, updated_at
```

不复制 review、decision、brain snapshot 或 governance JSON。每个样本必须回溯 source events，并保留现有 learning evidence contract。标签成熟应生成可追溯的新版本或投影更新，不能丢失原始 label source。

### 2.6 dataset_manifest

数据集只保存可复现 manifest：

```text
dataset_id, purpose, training_window, horizon_minutes
query_contract_hash, sample_digest, feature_schema_hash
label_contract_hash, target_source, config_hash
source_watermark, code_commit, artifact_hash, status, created_at
```

成员表只保存 `dataset_id`、`sample_id`、`sample_order`、`sample_digest`，不保存 features 或 source JSON。artifact 必须引用 dataset_id。

### 2.7 projection_run 和 legacy_mapping

`projection_run` 记录 projection/backfill 的 source watermark、代码版本、输入输出 digest、状态和错误；同一输入重跑必须幂等。

`legacy_mapping` 记录每个旧表/旧主键到 canonical event 或 quarantine 的映射、置信级别和原因，不允许静默丢弃。历史合并必须同时满足事件身份、producer、causation/correlation、entity、时间和 idempotency 证据，不能只按 JSON 相同合并。

## 3. 现有对象迁移归类

| 现有对象 | canonical_v2 处理 |
|---|---|
| broker execution、position lifecycle、trade review | 高可信 source facts |
| `decision_factor_snapshot` | 每次决策的 PIT 归因事实，不按 factor 名称去重 |
| `mutation_payload` | payload blob 来源，按稳定内容复用 |
| `evolution_decision`、`evolution_events`、`evolution_run` | occurrence/run lineage，不再保存重复完整 summary |
| `governance_mutation_intent` | reserve/commit/abort/effect lineage；before/target/rollback 改为引用 |
| `runtime_config_payload`、`runtime_config_snapshot` | payload blob + config version occurrence |
| `autonomous_learning_sample` | training sample projection，保留 evidence 和 source lineage |
| `brain_state_snapshot` | 历史 snapshot 证据，不升级为 canonical fact |
| `brain_memory` | 从 canonical facts 重建，不导入为事实 |
| `factor_catalog_snapshot` | projection 重建，不把旧 catalog JSON 当事实 |
| `state_payload_archive` | 校验后作为旧 payload archive 证据复用 |

## 4. 实施阶段

### Phase 0：冻结和基线

- 记录工作区、diff check、schema checksum、服务 MainPID、active writer、磁盘和数据库大小。
- 生成可验证的 PostgreSQL 逻辑备份，存储在工作区外。
- 建立 [`state-data-writer-registry.md`](state-data-writer-registry.md)，列出当前核心 INSERT/UPDATE/DELETE 入口并标注 fact、occurrence、projection、archive 或 unresolved。
- 旧 `state_v1` 只读；不运行 backfill、compact、VACUUM 或服务 scheduler。

完成条件：没有未记录生产 writer，备份可由 `pg_restore --list` 验证，旧 schema 可只读查询。

### Phase 1：schema-only foundation

由显式 `scripts/state_schema_migrate.py --apply` 应用 migration 16，创建 `canonical_v2` 及其表、外键、唯一键、索引和 checksum ledger。DDL 不读取历史 payload，不执行数据处理或物理重写；runtime connection 仍只校验 schema，不隐式建表。

### Phase 2：唯一写入 authority

共享 writer 只有四类：

1. `CanonicalPayloadWriter`：稳定 hash、幂等插入、压缩 metadata、restore 校验；
2. `CanonicalEventWriter`：事件身份、idempotency、不可变事件、causation/correlation；
3. `ProjectionMaterializer`：独占所属 projection、按 watermark 幂等重建；
4. `DatasetManifestWriter`：sample digest、feature/label/config/source lineage。

必须删除或禁止多模块各自保存完整 governance payload、event/run 重复 result、reader 默认 `persist=True`、immutable decision 覆写，以及 blocked/no-op action 的完整 unchanged state 复制。

### Phase 3：垂直链路

只先验证：

```text
decision -> broker execution -> position transition
         -> trade review -> learning sample
         -> dataset manifest -> training artifact
```

每个节点必须可通过 event ID、relation 和 payload hash 追溯。训练使用 server-side cursor 和预算保护，不触发 promotion、治理、V16 或 model registry，不启动 scheduler。

2026-08-15 已增加只读验证器
[`scripts/canonical_v2_vertical_shadow.py`](../../scripts/canonical_v2_vertical_shadow.py)。
对最新 50 条 `trade_outcome_review` 的实际运行结果为：50/50 条链路具备 entry decision、order lifecycle、position lifecycle 和 learning projection；选中 91 条旧 sample；没有写入；没有发现相同归一化 sample payload 候选。该结果是 shadow evidence，不代表已写入 canonical_v2 或已完成 dataset/artifact 生产链路。

随后做了真实 PostgreSQL 事务级 vertical smoke：6 个 payload、6 个 event、5 条 relation、1 个 training sample、1 个 dataset manifest 和 1 个 member 均通过约束；事务验证后显式 rollback，回滚后六张相关表均为 `0`，`writes_persisted=false`。这只证明 canonical writer/DDL/lineage 在 PG 上可用，不代表生产 writer 已切换。

同批 position-quality reader 的训练前置检查确认：`raw_candidate_row_count=34264`、`candidate_trace_count=13465`、`unique_review_bytes=36910849`，一条 position trace 窗口为 `137504928` bytes，超过 `134217728` bytes（128 MiB）预算并被排除；`trace_window_budget_exceeded=81` 条候选 trace 未进入 Python。安全窗口的 `selected_verdict_bytes=28532900`、`peak_buffered_bytes=769927`、`sample_count=106`，sample digest 为 `46afc0eed19beff3826833a1c6c68d04d46244b6eba50485670eec145a81d062`，feature schema hash 为 `1bca744fbd65a1d8f7138205011d2ec6b4741d5962f6977b25cf241decb2c69f`，label 为 negative `51` / positive `55`，target source 为 `closed_before_horizon` `52` / `trace_at_horizon` `54`。reader 为只读，`writes.database/artifact/model_registry` 均为 `false`。

第一次 training-only 终止审计仍保留为 `aborted_process_loss`，监控日志为 `/var/tmp/quant_training_only_20260816_1786813493.monitor.jsonl`。随后同一 `training_window_key=full:next_open:1786917660` 的有界恢复重试完成，audit `offmarket_position_quality_lightgbm:window:033b18f62abd541f95434f89` 最终为 `status=done`、`phase=finished`，sample `106`，sample digest `46afc0eed19beff3826833a1c6c68d04d46244b6eba50485670eec145a81d062`，semantic digest `1f487c6bb9bbf97244d090b70cbd951a983247a85625609beedcc42929451fd2`，feature schema `pit.v2.position_h30`，label negative `51` / positive `55`，target source `closed_before_horizon` `52` / `trace_at_horizon` `54`。artifact 为 [`position_quality_lightgbm_1786813897.json`](../../data/model_artifacts/position_quality_lightgbm/position_quality_lightgbm_1786813897.json)，SHA-256 `aa6bbd14edba0643630ec400ac78cf4d713990d7009fce1c1b69046ca08ed30f`；`model_registration=disabled`、`promotion=skipped`、`v16_delegate=skipped`。修正后的监控日志为 `/var/tmp/quant_training_only_retry_20260816_1786813893.monitor.jsonl`，观测 RSS `11243520` bytes、磁盘 `%util=0`、await `0`、swap in/out 未增长、checkpoint write/sync 增量 `0`、autovacuum `0`；但只有一个采样周期，完整峰值 RSS 标记为未充分观测。

### Phase 4：历史分批回填

顺序固定为：payload blob -> source events -> state versions -> governance lineage -> learning samples -> dataset manifests -> memory/catalog/readiness projections。

回填使用稳定主键游标、单批事务、可恢复 checkpoint；禁止 `fetchall()`。每批记录输入行数、输出 event、映射、quarantine、hash digest。重跑同批必须零重复 canonical event；unresolved 数据不进入训练和治理。

2026-08-15 已先完成只读 planner
[`scripts/canonical_v2_legacy_backfill.py`](../../scripts/canonical_v2_legacy_backfill.py)。完整 vertical 范围 dry-run 扫描 16,791 decision、1,357 order、2,956 position、721 review、58,632 sample；quarantine 为 0；只通过 `OCTET_LENGTH` 统计 sample JSON 约 768,038,702 bytes，未把大 JSON 载入 Python。发现 2,772 个 source 被两个不同 projection sample 引用（decision 2,152，review 620），这是 projection reuse，不直接等同于重复事实。当前只生成 mapping digest 和 checkpoint，不写 `legacy_mapping`。

同批只读检查还确认最大 `position_supervisor_trace` 的 81 条 verdict 均为不同 hash，并非相同事件的重复行；但 `evidence.supervisor_state.latest_supervisor` 递归嵌套旧 supervisor snapshot，造成 `137504928` bytes 的单 position trace 窗口。该问题属于旧 writer 的递归 payload bug，不通过删除数据解决。已在 `backend/services/supervisor_payload_contract.py` 和 `backend/ledger/service.py` 修复新写入边界；历史行未修改，必须在重新评估历史数据处理策略后才可进行任何 archive/compact。

随后使用现有 compactor 做了窄范围只读 repair manifest：
 [`data/state_payload_compact_maintenance_20260815_supervisor_review_dry_run.json`](../../data/state_payload_compact_maintenance_20260815_supervisor_review_dry_run.json)，SHA-256 为 `3f5deb9895feb499dfb9d9b0be62d01e95706a1e332fcd2ec6f20cfc163e2d29`。全量扫描 `position_supervisor_trace=47768` 行、`trade_outcome_review=721` 行，原始 JSON 合计 `434169813` bytes；exact raw payload duplicate 为 0；递归最大深度分别为 491 和 494；预计 gzip archive `76322219` bytes，预计临时空间 `510492032` bytes。该报告只读完成时记录的 coverage 是历史快照；当前静态 registry 为 `31 migrated / 0 pending`，但 apply manifest 必须重新生成，不能复用这份历史文件。
该报告只读完成时记录的 `consumer_coverage.all_migrated=false` 和 28 个 pending 是历史审计快照，不能代表当前代码。随后 archive loader 已扩展到当前 registry 的 31 个迁移项，当前静态覆盖为 `all_migrated=true`；上面的 manifest 仍是迁移前生成的历史审计文件，不能直接作为 apply manifest。compactor apply 仍未执行，必须先重新生成 manifest、复核资源和用户确认。

### Phase 5：projection rebuild

从 canonical facts 重建 `brain_memory`、factor catalog、readiness、API fact views、关系索引和 training sample projection。普通 `retrieve()` 纯读；显式 rebuild 才能写 projection_run 和 projection。第一阶段不接入外部 memory service。

### Phase 6：shadow read

新旧逻辑只读比较，不双写。比较 event/source coverage、payload refs、orphan refs、sample digest、feature/label/target/config contract、governance effect chain 和 memory source coverage，并区分“同事件”“同 payload 不同事件”“不同 projection”“unresolved”。

当前可重复的基础门为 [`scripts/canonical_v2_consistency.py`](../../scripts/canonical_v2_consistency.py)；它只验证已写入的 canonical_v2 内部一致性，不能替代 canonical rows 尚为空时的 legacy-to-v2 shadow diff。

### Phase 7：切换

停止旧 writer，记录旧 watermark，完成 v2 consistency check 后设置唯一 canonical authority 为 v2。禁止某模块继续写旧表或无限期双写。旧 `state_v1` 保持只读。

### Phase 8：旧库退役

至少保留 30 个自然日，并覆盖完整交易生命周期、治理回放、position-quality training-only 和 projection rebuild。满足条件后只归档旧 projection 和重复 payload；物理膨胀处理必须另行确认，不自动执行 `VACUUM FULL`，不删除 canonical source facts。

## 5. 资源和停止条件

迁移、backfill、训练全过程记录 RSS、MemAvailable、swap、磁盘 free/%util/await、PostgreSQL checkpoint write/sync、autovacuum 和 active transaction。

立即停止条件：RSS 连续三个周期上升或达到 MemTotal 80%；swap 连续三个周期增长；相关磁盘 `%util >= 90%` 连续三个周期；checkpoint write/sync 增量超过 60 秒；出现非预期 writer、长事务或阻塞。

停止方式：`SIGTERM`，等待 10 秒，仍未退出才 `SIGKILL`；保留 audit、monitoring log 和退出信息。

## 6. 测试和验收

新增针对性测试：

- `tests/test_canonical_v2.py`
- `tests/test_canonical_v2_vertical_shadow.py`
- `tests/test_canonical_v2_legacy_backfill.py`
- `tests/test_supervisor_trace_writer_bounds.py`

迁移、projection 和 training manifest lineage 合同集中由
`tests/test_state_schema_migrations.py`、`tests/test_canonical_v2.py` 和
`tests/test_position_quality_lightgbm.py` 覆盖，不另建重复测试文件。

必须验证：同 payload 单 blob；同 idempotency 重试不重复；同 payload 不错误合并真实事件；不同 payload 的同 key fail closed；immutable event 不可覆写；rollback 复用 before 引用；projection rebuild 幂等；memory read 无写入；backfill 可恢复且无静默丢失；dataset manifest digest 稳定；training-only 不触发治理或注册。

继续运行：

```text
tests/test_state_payload_archive.py
tests/test_state_payload_dedupe.py
tests/test_state_schema_migrations.py
tests/test_position_quality_lightgbm.py
tests/test_offmarket_high_load.py
tests/test_v16_read_only_brain.py
tests/test_autonomous_learning.py
tests/test_factor_autonomy_hardening.py
```

canonical_v2 只有在每个事实域一个 writer、payload 无跨表完整复制、sample/governance lineage 完整、projection 可删除重建、reader 纯读、历史回填幂等、shadow 差异全部分类、垂直链路和资源门通过后，才成为生产 authority。

## 7. 回滚和文档治理

切换前失败只处理未使用的 v2 产物，不触碰旧事实。切换后失败通过切回旧 authority、停止 v2 writer、保留 v2 审计并从最后成功 watermark 重建 projection 回滚；不改写历史 event。

本计划是 scoped companion，不替代 `docs/system-source-of-truth.md`。当前只更新 `docs/README.md` 入口和 `docs/legacy-debt-register.md` active debt；只有 v2 通过切换验收后，才更新长期 authority 文档。完成或取消后从活动入口移除本计划，事实转入 source-of-truth、rollout status 和 acceptance matrix。

## 8. 新对话接手点（2026-08-16）

本节是下一次对话的工作起点。先以本节和验收矩阵为准，再读取代码；不要依据旧 manifest 或旧对话中的“已完成”描述重新推断运行状态。

### 8.1 已确认的运行基线

最后一次只读复核结果：

```text
quant-backend.service MainPID=0
quant-learning-worker.service MainPID=0
pg_stat_activity active non-idle writer/query=[]
schema current_version=16, latest_known_version=16, migration_mismatches=[]
canonical_v2: 9 tables = 0 rows, orphan references = 0, writes_performed=false
/dev/vda2: 69G total, 44G used, 23G available, 66%
MemAvailable: approximately 2.5GiB
swap: approximately 236MiB used; no new swap growth observed in the successful training retry
```

已验证的 PostgreSQL custom-format backup 位于工作区外：
`/var/tmp/quant_state_v2_backup.u1Utjz/quant_audit_full.dump`，`pg_restore --list` 可读。工作区仍有大量用户未提交修改和新增审计文件，禁止 `reset`、`checkout`、覆盖文件和 `git add -A`。

### 8.2 已完成、但不能扩大解释的证据

- migration 16 只创建 `canonical_v2` schema 和约束；没有历史回填、删除、表重写或生产双写。`canonical_v2` 当前为空是预期状态，不是回填失败。
- PostgreSQL vertical smoke 在事务内验证了 6 payload、6 event、5 relation、1 sample、1 dataset 和 1 member；随后显式 rollback，相关表回到 0 行。它证明 DDL/约束/共享 writer 可用，不证明生产 writer 已切换。
- `position-quality` reader dry-run 和 training-only 已使用同一个样本选择逻辑。结果为 `sample_count=106`、`horizon=30`、feature schema `pit.v2.position_h30`、sample digest `46afc0eed19beff3826833a1c6c68d04d46244b6eba50485670eec145a81d062`；训练 artifact 未进入 model registry，promotion、governance、V16 和 shadow 均未执行。
- training-only 第一次因监控器误判只读 `FETCH` 被终止；同一 training window 只允许的一次恢复重试已完成。该训练的资源监控只有一个采样周期，因此“未 OOM”和“该次观测 RSS/IO/swap 未异常”已确认，但完整峰值 RSS 仍是未验证事项。
- supervisor/review consumer registry 当前为 `31 migrated / 0 pending`。这只代表代码读取覆盖，不代表旧 JSON 已 archive，也不代表可以删除 source event。
- 最新全量 compact dry-run manifest 只用于统计：[state_payload_compact_canonical_v2_fresh_20260816.json](</var/tmp/state_payload_compact_canonical_v2_fresh_20260816.json>)，SHA-256 为 `49a6aa31d9591cfc42ea0549cc592c0db335dd2b289382a485851b0ae0dc0c50`。它不是 apply manifest，不得被后续 apply 覆盖。

### 8.3 当前数据结论

已测得三类 payload 的逻辑重复规模：

| 域 | 行数 | distinct payload | 重复引用行 | 原始逻辑 bytes | 逻辑重复下界 |
|---|---:|---:|---:|---:|---:|
| `runtime_config_snapshot` | 14,597 | 3,582 | 11,015 | 5,414,094,369 | 3,438,962,257 |
| `brain_action_plan_eval` | 141,514 | 3,202 | 138,312 | 10,614,964,563 | 10,368,768,055 |
| `evolution_decision` | 44,734 | 35,787 | 8,947 | 10,830,956,243 | 3,871,719,716 |

解释边界：

1. 这些是同一 payload hash 的多次引用，不是同一业务事件的证明；真实发生在不同时间的事件必须保留 occurrence，只共享 payload。
2. `position_supervisor_trace` 和 `trade_outcome_review` exact raw payload duplicate 为 `0`。它们的主要问题是递归嵌套和 archive 压缩，不是按 hash 删除重复事件。
3. `audit_double_write` 为 `14,471` API audit rows、`30,263` canonical events、`linked=55`、`conflicts=5,927`、`unmatched=8,489`。这些必须进入 lineage 分类/quarantine，不能当作删除清单。
4. fresh dry-run 预计三域共享压缩 archive `76,322,219` bytes、预计临时空间 `12,716,065,737` bytes；这只是逻辑/临时空间估算，不是实际磁盘回收量。apply 前必须重新检查磁盘、临时空间、checkpoint、autovacuum 和 writer。

### 8.4 下一次对话的固定执行顺序

先执行只读基线，不启动任何服务：

```text
git status --short
git diff --check
systemctl show -p MainPID --value quant-backend.service
systemctl show -p MainPID --value quant-learning-worker.service
./.venv/bin/python scripts/state_query.py --sql "SELECT pid, state, wait_event_type, wait_event, LEFT(query, 240) AS query FROM pg_stat_activity WHERE datname=current_database() AND state <> 'idle' AND query !~* 'pg_stat_activity' ORDER BY pid"
./.venv/bin/python scripts/state_schema_migrate.py --check
./.venv/bin/python scripts/canonical_v2_consistency.py --max-payloads 1000
df -h /
free -h
swapon --show
```

然后按以下顺序继续：

1. 如果数据库内容、代码或运行态自上次 manifest 后有变化，重新生成一个新的 dry-run manifest；不得复用或覆盖旧 manifest。
2. 用有界/流式方法分类 `audit_double_write` 的 conflicts/unmatched；不得运行没有选择性条件的 API audit 与 canonical event 大 join，也不得把大 JSON `fetchall()` 到 Python。
3. 只有用户明确确认后，才执行第一批 payload archive/compact。使用新的 maintenance ID 和新的 apply manifest；示例命令如下，当前不执行：

   ```text
   ./.venv/bin/python scripts/state_payload_compact.py --apply --targets payload --maintenance-id maintenance_canonical_v2_payload_20260816 --batch-size 256 --manifest /var/tmp/state_payload_compact_canonical_v2_payload_apply_20260816.json
   ./.venv/bin/python scripts/state_payload_compact.py --verify --targets payload --manifest /var/tmp/state_payload_compact_canonical_v2_payload_apply_20260816.json
   ```

   `--apply` 前必须再次确认 MainPID=0、active writer 为空、pending mutation=0 和资源余量；`--rewrite`、`VACUUM FULL`、DELETE、历史 event 合并均不属于这一批。
4. payload verify 通过后，supervisor/review archive 仍作为独立范围、独立 maintenance ID 和独立 manifest 处理；验证 raw SHA-256、compressed/raw metadata、source row count、event occurrence 数量和 restore 后 JSON 语义。
5. legacy backfill apply、canonical vertical 写入、projection rebuild 和 authority cutover 必须另行记录 checkpoint、watermark、digest 和审计结果，不得和 compact 合成一条无保护命令。

### 8.5 交接时的禁止事项

- 不启动 `quant-backend.service` 或 `quant-learning-worker.service`；不启动 scheduler，不运行两个 scheduler 周期。
- 不执行 `state_payload_compact.py --apply`，除非用户明确确认当前范围、maintenance ID、manifest 和回滚边界。
- 不执行 `state_payload_compact.py --rewrite`、`VACUUM FULL`、历史 DELETE、旧表删除、outbox 清理或 brain/factor snapshot 清理。
- 不把 `brain_memory`、factor catalog、readiness 或 API projection 当作 canonical source fact。
- 不把“同 payload”报告成“同 event”，不把 source reuse 报告成可删除重复数据。
- 不把 migration 16、SQLite smoke、vertical shadow 或 consistency check 解释为 v2 已成为生产 authority。

### 8.6 仍未满足的验收门

| Gate | 当前状态 | 下一证据 |
|---|---|---|
| G3 vertical authority | shadow/rollback smoke 通过，production writer 未切换 | 一条真实、可回滚的 canonical vertical lifecycle，且无旧库双写 |
| G4 resource evidence | training semantic 通过，精确 peak RSS 未充分观测 | 可复用监控器或完整多周期资源审计 |
| G5 legacy backfill | planner 通过，quarantine=0 | 分批 apply、checkpoint 恢复和零重复验证 |
| G6 payload archive | fresh full dry-run 通过 | 用户确认后的 payload apply + verify；lineage 差异先分类 |
| G7 projection rebuild | pending | canonical source rows、watermark、input/output digest 和幂等重建 |
| G8 cutover/retirement | pending | 完整交易/治理/训练生命周期、旧 writer 删除证据和用户确认 |

下一次对话完成任何一个 gate 后，必须先更新本计划、验收矩阵和 legacy debt，再进入下一个 gate；不要只在聊天中报告状态。

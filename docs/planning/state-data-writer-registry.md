# state_v1 -> canonical_v2 writer registry

> Status: active migration evidence
> Last updated: 2026-08-16
> Scope: PostgreSQL state_v1 的核心事实、projection、payload 和 archive 写入口

这不是新的运行时 authority。它记录 shadow/backfill 阶段仍存在的旧写入口，避免把
“已经有 canonical_v2 shared writer”误报为“生产已经切换”。canonical_v2 当前只允许
隔离测试和后续显式离线 backfill 使用，不与 state_v1 双写。

| 入口 | 主要对象 | 分类 | 当前结论 | 退出/替代 |
|---|---|---|---|---|
| `backend/ledger/service.py` | `decision_ledger`、`order_lifecycle_event`、`position_lifecycle_event`、`position_supervisor_trace` | source fact / occurrence | 当前 live source writer；trace 新写入已使用 bounded hot projection + archive | 切换后改为 `canonical_v2.event` + `payload_blob`，旧表只读 |
| `alpha/reflection/reviewer.py` | `trade_outcome_review` | source fact | 当前 review writer；archive-aware，旧 inline 仅作 bounded projection | 切换后改为 `trade_review` event + payload reference |
| `backend/services/autonomous_learning.py` | `autonomous_learning_sample`、`evolution_events`、部分 review/trace repair | projection / legacy repair | 仍是旧 learning projection writer；不作为 canonical fact | 由 canonical source event + explicit projection materializer 替代 |
| `backend/services/learning_backfill.py`、`scripts/backfill_*.py` | review、decision、position、sample | offline repair/backfill | 仅在显式维护任务中运行；不是 live authority | canonical backfill 完成后退役或改为 adapter |
| `scripts/repair_open_ledger_from_deals.py`、`scripts/repair_decision_temporal_context.py` | decision/order/position legacy rows | one-off repair | 历史修复入口，不能作为 canonical writer | 完成历史核对后封存 |
| `backend/services/state_payloads.py`、`backend/services/evolution_ledger.py` | `mutation_payload`、`evolution_run` | legacy payload/run | 旧 governance/evolution payload authority | 改为 `payload_blob` + event/relation/state_version |
| `backend/services/v16_brain_snapshot.py` | `brain_state_snapshot`、`brain_memory` | snapshot / rebuildable projection | `brain_memory` 只能作为可重建索引，不是事实源 | 从 canonical facts 显式 rebuild |
| `backend/services/factor_catalog.py` | `factor_catalog_snapshot` | catalog projection | 旧 catalog snapshot 仍可读写 | 从 factor events/state versions rebuild |
| `backend/services/state_payload_archive.py` | `state_payload_archive` | archive | 旧 state_v1 archive writer，保留 hash/恢复校验 | 迁移后可复用为 legacy evidence，不做事实源 |
| `scripts/state_payload_compact.py` | supervisor/review archive 与旧 payload compact | explicit maintenance | `--apply` 未执行；不自动 VACUUM FULL | 用户确认后分阶段执行，source facts 不删除 |
| `backend/services/canonical_v2.py` | `canonical_v2.*` | shared canonical authority | 当前无生产调用方；contract tests 已覆盖 | 完成垂直链路和切换门后成为唯一 writer |

## 当前未登记为生产 writer 的路径

- [`scripts/canonical_v2_legacy_backfill.py`](../../scripts/canonical_v2_legacy_backfill.py) 只读扫描稳定主键、校验 source 引用并输出 digest，不写 `legacy_mapping`。
- [`scripts/canonical_v2_vertical_shadow.py`](../../scripts/canonical_v2_vertical_shadow.py) 只读比较旧链路，不写 canonical rows。
- [`scripts/canonical_v2_consistency.py`](../../scripts/canonical_v2_consistency.py) 只读检查表、引用 orphan 和 payload hash/恢复完整性。

生产切换前，必须再次用源码扫描、测试和运行态 writer 检查确认本表没有遗漏；发现新的
INSERT/UPDATE/DELETE 入口时先补 registry，再决定是否允许切换。

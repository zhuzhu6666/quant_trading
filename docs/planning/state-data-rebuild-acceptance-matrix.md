# canonical_v2 数据重建验收矩阵

> Status: active acceptance matrix
> Last updated: 2026-08-16
> Scope: `state_v1 -> canonical_v2` 的可重复证据，不替代架构计划

| Gate | 可重复证据 | 当前状态 | 未满足项 |
|---|---|---|---|
| G0 冻结与 schema | `state_schema_migrate.py --check`、备份 `pg_restore --list`、MainPID、active writer | `pass` | 无；旧库仍只读目标 |
| G1 writer/reader registry | [`state-data-writer-registry.md`](state-data-writer-registry.md)、compactor consumer coverage | `pass`（31 migrated / 0 pending） | canonical 尚未切换为生产 writer |
| G2 canonical DDL/refs | [`canonical_v2_consistency.py`](../../scripts/canonical_v2_consistency.py) | `pass`（当前九表为空、orphan=0） | 尚无回填 rows 可做全量 payload 验证 |
| G3 vertical shadow/writer smoke | `canonical_v2_vertical_shadow.py --limit 50` + PostgreSQL rollback smoke | `pass`（50 complete / 0 incomplete；PG 6 payload/6 event/5 relation/1 sample/1 dataset，rollback 后 0 rows） | 仍未切换 production writer；smoke 不留下 canonical rows |
| G4 training reader | `inspect_training_window(limit=4000,horizon=30)` + `run_offmarket_position_quality_job(execution_mode='training_only')` | `pass`（reader/training semantic，sample=106） | 1 个 position trace window（81 条 trace）因 `137504928 > 134217728` 在 SQL 预算阶段排除；training resource monitor 只采到 1 个周期，完整 peak RSS 仍需后续可复用监控器补强 |
| G5 legacy backfill planner | `canonical_v2_legacy_backfill.py --batch-size 256 --max-rows-per-table 100000` | `pass`（quarantine=0，source EXISTS 校验通过） | apply、checkpoint 写入和 canonical event 生成未执行 |
| G6 payload archive/compact | `state_payload_compact.py --dry-run --targets all --batch-size 256 --chunk-size 4096`；fresh manifest `/var/tmp/state_payload_compact_canonical_v2_fresh_20260816.json`，SHA-256 `49a6aa31d9591cfc42ea0549cc592c0db335dd2b289382a485851b0ae0dc0c50` | `pass`（全量只读统计：三域逻辑 payload 26.86GB、逻辑重复下界 17.68GB；监督/review 434MB，exact duplicate=0；consumer=31/0） | 用户确认前禁止 apply、删除、VACUUM FULL；`audit_double_write` 尚有 5,927 conflicts / 8,489 unmatched 需分类 |
| G7 projection rebuild | 显式 `projection_run` + source watermark/output digest | `pending` | canonical source facts 尚为空 |
| G8 cutover/retirement | v2 consistency、完整交易/治理/训练生命周期和旧 writer 删除证据 | `pending` | 不得双写；旧 state_v1 暂不退役 |

矩阵中的 `pass` 只代表对应门的证据成立，不代表整个 canonical_v2 已成为生产 authority。

## 交接固定事实（2026-08-16）

- backend/worker MainPID 均为 `0`；最后一次 active writer 查询为空；schema 为 `16`；canonical_v2 九张表均为 `0` 行。
- 未执行 compact apply、历史 backfill、DELETE、`VACUUM FULL`、物理 rewrite、服务启动或 scheduler。
- fresh 全量 dry-run manifest 为 [`/var/tmp/state_payload_compact_canonical_v2_fresh_20260816.json`](</var/tmp/state_payload_compact_canonical_v2_fresh_20260816.json>)，SHA-256 `49a6aa31d9591cfc42ea0549cc592c0db335dd2b289382a485851b0ae0dc0c50`；该文件只读统计，后续 apply 必须使用新的 manifest，不能覆盖它。
- `brain_action_plan_eval` 的 `138,312` 条重复引用行只能作为 payload intern/archive 候选；不能直接删除 event occurrence。监督/review exact duplicate 为 `0`，主要风险是递归 payload 膨胀。
- `audit_double_write` 的 `5,927 conflicts / 8,489 unmatched` 仍未完成 lineage 分类，是 compact/回填前的阻塞审计项，不是删除数量。
- 下一步必须从只读预检查开始；用户明确确认后才可执行 payload 范围 apply，之后独立 verify，再考虑 supervisor/review archive。G7/G8 仍 pending。

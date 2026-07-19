# 分期修复故障与验收矩阵

> Status: active release evidence
> Snapshot: 2026-07-19 14:12 CST
> Scope: Phase 0-5 fault injection, governance, frontend, and remaining live gates

本文只记录可重复执行的证据映射。单元/集成测试证明故障语义；真实
broker freshness、shadow continuity 和完整持仓生命周期必须由生产 observation
ledger 证明，二者不能互相替代。

## 1. 执行与 Safety 故障矩阵

| 计划场景 | 权威测试证据 | 必须保持的结果 |
| --- | --- | --- |
| closed bars 缺失、factor 异常、circuit open | `test_phase2_runs_broker_snapshot_and_safety_before_missing_bars`、`test_phase2_circuit_blocks_alpha_only_after_safety`、`test_merge_portfolio_configs_fails_closed_when_factor_admission_raises` | alpha 阻断，safety 先执行 |
| PostgreSQL 启动/运行失败 | `test_close_context_postgres_failure_records_outbox_and_continues`、`test_close_v2_pg_intent_failure_does_not_block_risk_reduction`、`test_latch_and_outbox_persistence_failure_still_allows_emergency_close` | 新风险 fail-closed，close/reduce/emergency 继续 |
| account/positions reconcile 失败 | `test_account_reconcile_failure_blocks_alpha_but_safety_still_runs_first`、`test_reconcile_failure_blocks_new_risk_without_suppressing_future_safety`、`test_final_open_admission_blocks_stale_or_newer_failed_reconcile` | 不把失败编码为空仓或新鲜账户 |
| spot stale | `test_final_open_admission_fails_closed_for_each_authority[postgres2-session2-quote2-spot_quote_stale]` | 最终 open admission 拒绝 |
| order timeout、延迟回执、未知 protobuf | `test_timeout_does_not_guess_existing_same_direction_position`、`test_loop_recovers_delayed_fill_and_runs_safety_before_alpha`、`test_v2_unknown_protobuf_with_position_shaped_fields_is_not_a_broker_receipt` | outcome=unknown，禁止重发和新增风险 |
| amend accepted 但 broker 未更新 | `test_amend_v2_requires_fresh_sltp_projection_ack`、`test_tighten_accepted_rpc_requires_matching_fresh_broker_projection` | 不报告 confirmed |
| emergency pre/post reconcile 失败 | `test_emergency_requires_fresh_pre_reconcile`、`test_emergency_post_reconcile_failure_never_reports_success` | 不报告 completed/no_positions |
| stop 与 open RPC 并发、draining start | `test_stop_waits_for_admitted_open_rpc_then_keeps_generation_draining`、`test_draining_generation_keeps_thread_ownership_and_rejects_replacement` | 单 generation ownership |
| runtime_kv 缺失、损坏、跨日 | `test_invalid_cache_never_zeros_last_known_risk_or_opens_new_risk` 三个参数实例 | session 不归零，不开放新风险 |
| partial close 后 position 仍开放 | `test_partial_close_legs_aggregate_by_position_and_open_position_is_excluded`、`test_recovery_requires_fresh_expected_partial_close_volume` | completed trade 排除仍开放 position |
| audit/outbox 写入失败 | `test_safety_outbox_failure_never_changes_risk_reduction_result`、`test_close_broker_success_survives_all_post_broker_audit_failures` | broker 风险缩减结果不被审计失败改写 |
| safety heartbeat 超过 15 秒 | `test_stale_safety_heartbeat_degrades_generation_and_blocks_new_risk`、`test_watchdog_violation_durably_latches_no_new_risk` | 持久化 no_new_risk，保护继续 |

## 2. 治理与 Worker 矩阵

| 计划场景 | 权威测试证据 |
| --- | --- |
| reserved/prepared/事务内故障 | `test_fault_before_commit_aborts_without_overlay_change` 的三个故障点 |
| 双 worker 同 scope | `test_concurrent_scope_reservation_has_one_owner` |
| commit 后 publish 前失败与恢复 | `test_publish_failure_is_degraded_and_replayable_without_recommit` |
| V16 只 finalize 一次 | `test_v16_apply_count_increments_only_once_at_finalize` |
| live 不消费 approved | `test_live_never_consumes_approved_and_dual_mode_quarantines_legacy` |
| ACTIVE factor 完整证据 | `test_activation_requires_fresh_bound_projection_and_health`、`test_discovered_factor_without_explicit_weight_never_gets_implicit_default` |
| contaminated/partial eligibility 为零 | `test_sample_upsert_persists_full_recovered_and_contaminated_eligibility`、`test_governor_rejects_missing_contract_and_uses_weighted_metrics` |
| legacy backtest 不进入治理 | `test_legacy_evidence_is_zero_weight_even_if_flags_are_spoofed`、`test_legacy_parameter_candidate_cannot_be_approved_or_deployed` |
| PG job claim/lease/concurrency/retry | `tests/integration/test_postgres_job_queue.py` 六项真实 PostgreSQL 隔离 schema 测试 |

## 3. Frontend/Auth 矩阵

- Web `_fact` unknown/stale/error、非绿色、stop/emergency escape hatch：
  `web_frontend/src/tests/fact-behavior.test.mjs`、`fact-auth.test.mjs`。
- 并发 401 单次注销与 WS/cache 清理：`fact-auth.test.mjs`。
- recovery 未注册为 unknown：
  `test_readiness_warming_and_unregistered_recovery_are_explicitly_unknown`、
  `test_ops_recovery_route_marks_unregistered_monitor_unknown`。
- 小程序 all-failed 不推进 `lastSuccessAt`：
  `tests/miniprogram_store_reducer.test.mjs`。
- emergency 不增加二次密码阻碍：
  `test_expired_access_can_emergency_when_pg_audit_is_unavailable`。

## 4. 当前执行结果

- 2026-07-19 14:12 CST，执行/Safety/治理/研究/worker 显式矩阵：
  `269 passed`。
- spot/factor/fact/auth 补充矩阵：`81 passed`。
- PostgreSQL job queue 隔离 schema：`6 passed`，未以 SQLite 替代。
- 全量发布门此前为 `2231 passed, 9 skipped`；唯一 façade 行数门修复后对应
  回归已通过。

## 5. 不能由测试替代的剩余证据

`scripts/safety_shadow_gate.py --required-hours 24` 必须最终满足以下二选一：

1. 连续 24 小时 broker-confirmed 空仓 shadow observation；或
2. 至少一个完整 broker position lifecycle。

窗口内每条记录都必须同时满足 fresh reconcile、account/positions age 不超过
15 秒、unknown execution 为零、independent exact match、无 duplicate/conflict、
无 forced shadow，且相邻 full-cycle 间隔不超过 75 秒。门未通过前 Safety 保持
`shadow`，Generation/Execution outcome/PG job queue 不推进。

阶段切换前还必须执行：

```bash
.venv/bin/python scripts/phased_repair_release_gate.py --target safety_enforce
```

该命令必须以 0 退出，并同时证明静态 flags 仍处于预期前态、两个服务 active、
latch cleared、本地/PG unresolved intent 均为 0、持久化 readiness 新鲜、
release/autonomous mutation ready、worker config/overlay hash 一致。快照过期或任一
事实不可读都返回非零；该脚本本身不会修改开关或重启服务。

# 交接提示词（2026-08-18 停机重建中）

> 本文件是下次会话的启动上下文。新对话开头直接贴入即可。

---

## 系统状态（2026-08-18 实测）

- **双服务已停**：quant-backend `inactive`（drop-in `zz-stop.conf` 已设 Restart=no；重启服务前需删除该文件 + `daemon-reload` 恢复 Restart=always）、quant-learning-worker `failed`
- **端口 8000 释放、无 docker 容器、PG 无写者**
- **迁移账本已重建**：`state_schema_migration` 18 条 applied（ok=True, version=18），`migrations/state_pg/` 18 文件与 catalog 一致（0019 孤儿文件已移出归档到 `run_artifacts/final_execution_baseline_20260818/`）
- **全量回归**：2804 passed / 1 known-environmental（`state_v1.runtime_config_payload` 缺唯一约束——旧库残缺，**清库自愈，不修**）/ 12 skipped
- 磁盘 36G/69G（55%）；canonical_v2 数据：event 126,813 / payload 141,931 / sample 58,640 / state_version 2,556 / legacy_mapping 154,424

## 用户已拍板决策（不可回退）

1. **全库清空重建**：`state_v1`（v1）直接删除、`canonical_v2`（v2）**数据全部清空**——不保留任何历史数据，只留 schema + 代码
2. **不备份、不关心历史数据**
3. 保留仅结构：canonical_v2 9 表 schema（**除 legacy_mapping 表，整体移除**）+ 运行态表（overlay/snapshot/jobs/auth_session/auth_revocations/runtime_kv）+ migration 账本 → **迁独立 runtime schema（保留结构、清空数据）**
4. 样本表删、记忆清空重建
5. 前端（miniprogram_v2 / web_frontend）不涉及（Windows 本地，服务器后端-only sparse）
6. **服务启停/物理删库由用户在自己 root 终端执行**（当前 DSH 沙箱无 root、`/etc` 只读、sudo 被禁；但沙箱有 sudo 组时可执行 systemctl）

## 蓝图与执行顺序

- 终极清单：`docs/planning/final-execution-checklist.md`（全库清空版，唯一蓝本）
- 架构审计+问题点：`docs/planning/architecture-audit-2026-08-18.md`（§7.6 A 类 6 项结构断链 + B 类 10 项 + R 系列多余设计）
- 顺序：**S0 冻结 → S1 账本 → S2 公共层+清扫 → S3 代码单轨+结构修复(A1-A6+B1/B2/B5) → S4 验证 → S5 清库 → S6 容量阀 → S7 启动+进化闭环首验**
- 硬约束：A 类修复必须先于清库；账本不修则服务重启即挂

## 已完成进度（本轮）

| 阶段 | 状态 |
|---|---|
| S1 账本修复+可回归性 | ✅ 账本 18 applied、migrations 测试修复、回归基线 2804/1 |
| S2.1 db_helpers 公共层 | ✅ `backend/core/db_helpers.py`（conn_is_pg/pg_sql/execute/load_json/dump_json/row_value）+ 6 单测；**33 文件收敛，删 ~60 本地 def**；全编译 0 失败 |
| S2.2 四域清扫核心 | ✅ 删 **9 空壳**（brain_*×7+agent_authority_registry+agent_governance，含 research/ 引用修复）+ **EvolutionKernel**（system_health 统一注册）+ **run_autonomous_factor_governance_cycle**（零调用）+ **v16_command_gate.consume()**（直调 finalize） |
| S2.3 写者收敛 | ⏳ 待做 |

已评估保留（非死代码/敏感区，不强删）：`partially_matured` 状态、`build_trade_lesson`（测试依赖 fallback）、`evolution_ledger` SQLite DDL（测试 fixture，归 S3/R5）、`GovernanceExpansionControlService`（2 端点真实控制面）。

## 当前代码改动

- **~60 backend 文件改动**（全部收敛/删除，无逻辑变更，每批针对性测试绿）
- 关键新文件：`backend/core/db_helpers.py`；`tests/test_db_helpers.py`
- 已删文件：9 空壳 + `backend/runtime/evolution_kernel.py`
- git：大量未提交改动——**禁止 reset/checkout 覆盖**

## 下一步（S2.3 起）

1. **S2.3 写者收敛**：`experience_pattern_stats` 单写者（policy_suggester 为权威）；`FactorWeightChangeService` 5 调用方统一 producer/source
2. **S3 代码单轨 + 结构修复**（核心）：
   - A1 `record_trade_review_event` 实时写入器（**全清后后验/先验唯一前提**）；A2 label 单一口径（reviewer.py positive_share 为权威）；A3 posterior 触发；A4 win 正反馈；A5 effect 链；A6 trace 成熟链
   - B1 归因排除列表补 execution_timing；B2 责任域枚举统一；B5 删 dimension_evidence 死参数
   - legacy 读取全切 canonical（autonomous_learning_sample 直读直写 60+ 处）；R1 legacy_mapping 路径移除；R2 15 脚本退役（留 3）；R5 测试夹具 canonical 化
3. **S5 清库（用户执行）**：运行态表+账本迁 runtime schema → v1 事实表 DROP + public audit 4 表 DROP → v2 10 表 TRUNCATE → legacy_mapping DROP → /var/tmp 旧 dump 删除
4. 恢复服务：删 `zz-stop.conf` + `daemon-reload`（或直接 start）

## 关键文件索引

- `docs/planning/final-execution-checklist.md`（终极执行清单）
- `docs/planning/architecture-audit-2026-08-18.md`（8 域审计 + 问题点 A/B/R + 记忆后验深挖）
- `run_artifacts/final_execution_baseline_20260818/`（S0 基线 + 每轮进度 + 0019 归档）
- `backend/core/db_helpers.py`（新公共层）、`backend/core/state_schema_migrations.py`（catalog 18、baseline 已清空）
- `backend/services/canonical_v2.py` / `canonical_v2_reader.py`（canonical 写/读）

## 纪律提醒

- **服务启停/写库/删表/物理操作由用户执行**（当前环境无 root，但沙箱有 sudo 组时可执行 systemctl——drop-in `zz-stop.conf` 是停机关键，别误删）
- **不得为将删的旧表修约束凑测试绿**（runtime_config_payload 缺约束是清库自愈项）
- **A1 trade_review 写入器必须先于清库就绪**（否则重建后评审事实源断链）
- 每批跑针对性测试；全量回归在阶段收口时跑（基线 2804/1）
- 替性：`_sql`/`_execute` 等已收敛到 db_helpers，新代码一律从 `db_helpers` 导入，**禁止再复制本地变体**

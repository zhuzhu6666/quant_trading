-- 0032_restore_jobs_primary_key.sql
-- D12 audit-defects-2026-08-21 增补 - runtime.jobs 缺主键
--
-- 标准 STATE_DB_DDL 合同是 id TEXT PRIMARY KEY, 见 backend/core/db.py。
-- S7.3 重建出的 jobs 表没有任何主键或唯一约束, 只有 idx_jobs_kind_idempotency
-- 部分唯一索引, 导致两条兼容路径的 ON CONFLICT id upsert 直接报 planner 错误:
-- backend/jobs/manager.py _append_persisted 与 autonomous_learning 的
-- parameter_template_validation 兼容投影。当前 pg_job_queue_v2_enabled 关闭
-- 且表 0 行故未爆发, 开关一开即炸。
-- v2 队列写入者 pg_queue.py enqueue 用的是 ON CONFLICT kind + idempotency_key,
-- 与部分唯一索引匹配, 不受影响。
--
-- 本迁移: 补回标准合同主键, 表当前 0 行无数据风险。
-- 兼容两种起点: 生产表无任何约束时 DROP 跳过, 全新重放基线的建表语句
-- 已带内联主键时先 DROP 再 ADD 避免重复主键报错。

ALTER TABLE runtime.jobs DROP CONSTRAINT IF EXISTS jobs_pkey;

ALTER TABLE runtime.jobs
    ADD CONSTRAINT jobs_pkey PRIMARY KEY (id);

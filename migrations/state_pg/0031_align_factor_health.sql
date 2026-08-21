-- 0031_align_factor_health.sql
-- D11 (audit-defects-2026-08-21 增补): runtime.factor_health 表合同对齐
--
-- 根因: S7.3 重建时按 _PG_BUSINESS_TABLES_DDL 极简版建表, 主键落在 factor_id
-- 并带 health_score 双套列, 而唯一写入者 alpha/factor_health.py write_report
-- 与全部读者(factor_catalog / factor_cards / factor_blend_health /
-- factor_pruning_candidates / factor_lifecycle_service / backend_readiness /
-- learning_worker)均按标准合同(backend/core/db.py STATE_DB_DDL)读写
-- factor 主键 + score 列. ON CONFLICT(factor) 无匹配唯一约束导致每次
-- evolution cycle 落库必炸, 异常被 logger.debug 吞掉, 表自重建以来 0 行.
--
-- 本迁移: 表当前 0 行(已核实), 直接删两套死列并把主键改到 factor,
-- 同时补齐标准合同的缺失列(section), 不保留旧列兼容.

ALTER TABLE runtime.factor_health DROP CONSTRAINT IF EXISTS factor_health_pkey;

ALTER TABLE runtime.factor_health DROP COLUMN IF EXISTS factor_id;
ALTER TABLE runtime.factor_health DROP COLUMN IF EXISTS health_score;

ALTER TABLE runtime.factor_health ADD COLUMN IF NOT EXISTS section TEXT NOT NULL DEFAULT 'unknown';

ALTER TABLE runtime.factor_health
    ADD CONSTRAINT factor_health_pkey PRIMARY KEY (factor);

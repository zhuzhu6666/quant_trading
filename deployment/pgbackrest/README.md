# PostgreSQL `state_v1` 灾备合同

本目录只提供可审计的安装模板；它不会安装 pgBackRest、修改 PostgreSQL、创建 S3 bucket、创建 stanza、启用 timer，或执行 restore。运行环境目前未提供对象存储凭据时，必须保持这些 unit 未启用。

## 固定边界

- 唯一业务事实仍是 PostgreSQL `state_v1`；pgBackRest 只备份整个 PostgreSQL cluster，不创建第二份业务状态。
- pgBackRest 是备份/WAL 的权威来源；`runtime_kv.postgres_backup_health.v1` 仅保存脚本的脱敏观察，readiness/API 不会拿它改变交易、治理或发布权限。
- `experience_memory` 是可重建投影，`brain_memory` 是有界检索索引；恢复验收必须同时检查原始复盘、经验投影和索引引用。
- 禁止对生产库自动 restore、promote、切换 DSN 或清理数据目录。

## 首次受控启用

1. 安装与 PostgreSQL 16 兼容的 pgBackRest；创建已加密、版本化且禁止公开访问的 S3 兼容 bucket。
2. 以 `pgbackrest.conf.example` 为模板创建 `/etc/pgbackrest/pgbackrest.conf`，并以 `pgbackrest.env.example` 创建 `/etc/quant/pgbackrest.env`；将 cipher passphrase、对象存储凭据和 state DSN 放入受保护的 host 配置，权限为 `0600`。优先使用 workload/instance role，避免长期 shared key。
3. 用 `SHOW data_directory` 填写 `pg1-path`，用 `SELECT current_setting('config_file')` 确认 PostgreSQL 配置文件；先由 operator 审核后再设置：

   ```conf
   archive_mode = on
   archive_command = 'pgbackrest --stanza=quant-state-v1 archive-push %p'
   ```

   `archive_mode` 的变化需要按 PostgreSQL 运维流程重启；不可在交易时段直接变更。
4. 以 `postgres` 用户执行 `stanza-create`、`check`、首次 `--type=full backup`，确认 `pgbackrest --stanza=quant-state-v1 --output=json info` 的 stanza 状态为正常，且 `pg_stat_archiver` 有成功归档。
5. 仅在上一步成功后安装这两个 service/timer，并执行 `systemctl daemon-reload`、`enable --now quant-pgbackrest-full.timer quant-pgbackrest-diff.timer`。运行脚本会把脱敏结果发布到 `runtime_kv.postgres_backup_health.v1`。

## 隔离恢复演练

1. 在独立 PostgreSQL 实例/主机恢复，使用不同的数据库 DSN；不要对生产 data directory 运行 restore。
2. 备份前运行 `scripts/capture_state_backup_manifest.py --output <operator-manifest.json>`，把此 manifest 与同一备份集一并保存在受控对象存储。
3. restore 完成、但未启动为生产服务前，运行：

   ```bash
   .venv/bin/python scripts/verify_state_restore.py \
     --restored-dsn '<isolated-dsn>' \
     --expected-manifest <operator-manifest.json> \
     --confirm-isolated
   ```

   工具会核对 state schema、三个关键表的行数和 `MemoryIntegrityReport`。任何差异都会非零退出并列出差异；它不执行 promote。
3. 演练已在隔离实例成功后，如需让现有运行健康投影显示最近一次演练，可在**运行生产 health projection 的受控主机**额外传入 `--publish-production-health`。它只写入脱敏的演练时间、schema/行数/记忆检查状态；不会连接生产库做 restore、不会 promote，也不会变更交易权限。未记录成功演练时，即使有新备份，健康投影仍明确为 `degraded`。

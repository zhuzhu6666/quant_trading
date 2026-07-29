# Windows 主动拉取 PostgreSQL 备份

这是当前 Demo 的唯一异地备份路径：Windows 电脑在线时主动通过 SSH 拉取一个完整的 `quant_audit` 逻辑快照。服务器不保存备份文件，不启用 WAL archive、pgBackRest repository、S3 或 systemd timer。

## 固定边界

- 备份内容是 PostgreSQL `quant_audit`（含 `state_v1`）；代码仍由 GitHub 保存，K 线/事件/外部 DuckDB 数据和 `.env` 不在本批备份内。
- 服务器只把 `pg_dump --format=custom --compress=zstd:3` 写入 SSH 标准输出；Windows 用 `pg_restore --list` 验证后才原子改名并发送回执。
- `quant-backup-pull` 是 locked Linux account。其 Windows key 被 forced command 限制为 `dump`、已校验格式的备份回执和恢复演练回执，不能获得 shell、端口转发或任意 sudo。
- 每个完成的 Windows 文件默认至少预留 20GiB，本地保留最新 7 份。不要人为截断 dump；磁盘不足时应失败并保留旧文件。
- 这是离线逻辑快照，不支持任意时间点恢复。恢复点就是上一次 Windows 成功拉取的时间；`verify_state_restore.py` 只核对隔离恢复后的 schema 和记忆完整性，不伪造与在线源的逐行一致性。

## 服务器准备

本仓库管理员执行一次：

```bash
sudo deployment/windows-backup/install_server_endpoint.sh
```

这一步不会安装 Windows 公钥、执行 dump、建立 timer、修改 PostgreSQL 或占用额外备份空间。

## Windows 一次性准备

1. 安装 WSL2 Ubuntu 和 PostgreSQL client：`sudo apt-get install postgresql-client`。
2. 生成专用 key：`ssh-keygen -t ed25519 -f ~/.ssh/quant_state_backup`。
3. 只把 `~/.ssh/quant_state_backup.pub` 发给服务器管理员；私钥永远留在 Windows。
4. 管理员执行：

   ```bash
   sudo deployment/windows-backup/install_windows_public_key.sh /path/to/quant_state_backup.pub
   ```

5. 将本目录的 `pull_quant_state.sh` 放入 WSL，建立服务器 host key 的 `known_hosts` 条目后运行：

   ```bash
   RETAIN_COUNT=7 MINIMUM_FREE_GIB=20 ./pull_quant_state.sh \
     quant-backup-pull@<server-host> /mnt/d/quant-state-backups ~/.ssh/quant_state_backup
   ```

首次成功后，再在 Windows Task Scheduler 设置“登录后每天一次”。不要使用 `StrictHostKeyChecking=no`，不要把私钥或 `.env` 放入仓库。

## 隔离恢复

在 Windows WSL 或另一台隔离 PostgreSQL 上新建空库，然后恢复：

```bash
createdb quant_audit_restore
pg_restore --no-owner --no-privileges -d quant_audit_restore <dump-file>
.venv/bin/python scripts/verify_state_restore.py \
  --restored-dsn '<isolated-dsn>' \
  --confirm-isolated
```

只有上一步成功（退出码为 0）后，才从 Windows WSL 回报此次演练：

```bash
ssh -i ~/.ssh/quant_state_backup -o BatchMode=yes -o StrictHostKeyChecking=yes \
  quant-backup-pull@<server-host> restore-receipt "$(date +%s)"
```

该回执明确标记为 `windows_client_reported`；它不代表服务器读取过离线 Windows 磁盘。禁止对生产 DSN 运行 restore 或 verify。

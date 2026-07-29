# Server Backend SOP

> Status: active
> Last verified: 2026-07-26
> Scope: Linux unified workspace startup, logs, PostgreSQL, cTrader, frontend build, restart, and runtime acceptance.

这份文档只服务一个目标：
把服务器上的后端日常操作标准化，减少临场判断和误操作。

## 1. 适用范围

这份 SOP 适用于：

- 后端接口排查
- 交易循环排查
- cTrader 连接问题
- `.env` / systemd / 日志 / 数据库问题
- 服务器热修

Web 和小程序源码也可在 Linux 统一工作区修改；微信开发者工具与 Windows 浏览器只承担平台专属补充验证。

## 2. 基础信息

当前服务器：

- IP: `124.221.7.195`
- SSH User: `ubuntu`
- Project Root: `/home/ubuntu/quant_trading`
- Service: `quant-backend.service`
- Public domain: `www.zhuzhu666.icu`
- Public reverse proxy: `caddy.service`
- Backend bind: `127.0.0.1:8000`

## 3. 登录后第一步

SSH 进入服务器后，先执行：

```bash
cd /home/ubuntu/quant_trading
pwd
git status --short
git rev-parse --short HEAD
systemctl is-active quant-backend.service
systemctl is-active caddy.service
```

目的：

- 确认当前目录正确
- 确认工作区是否脏
- 确认当前代码版本
- 确认后端服务是否还活着
- 确认公网反代是否还活着

## 4. 日志排查顺序

排查问题时默认按这个顺序：

1. 看服务状态
2. 看最近日志
3. 看接口健康
4. 再决定是否改代码

### 服务状态

```bash
systemctl status quant-backend.service --no-pager -n 30
```

### 最近日志

```bash
journalctl -u quant-backend.service -n 100 --no-pager
```

看最近 5 分钟：

```bash
journalctl -u quant-backend.service --since "5 min ago" --no-pager
```

### 关键词过滤

```bash
journalctl -u quant-backend.service --since "10 min ago" --no-pager | egrep "ERROR|WARNING|Traceback|ctrader|auth failed|401|403|500"
```

## 5. 健康检查

### 最小健康检查

```bash
curl http://127.0.0.1:8000/api/health
curl https://www.zhuzhu666.icu/api/health
```

### 登录检查

不要在文档里硬编码真实密码。登录验证时从本机环境变量或临时输入读取：

```bash
read -rsp "Password: " QUANT_LOGIN_PASSWORD; echo
curl -X POST http://127.0.0.1:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"zhu\",\"password\":\"${QUANT_LOGIN_PASSWORD}\"}"
unset QUANT_LOGIN_PASSWORD
```

### 常用业务接口

多数业务接口需要 JWT。未带 token 时返回 `missing_authorization` 是预期行为，不代表接口挂了。

```bash
curl http://127.0.0.1:8000/api/health
```

如果需要带 JWT，就先登录再带 `Authorization: Bearer ...` 调用：

- `/api/live/account`
- `/api/live/positions`
- `/api/live/strategy-status`
- `/api/live/session-stats`
- `/api/risk/summary`

### 公网入口 / Caddy 检查

当前公网入口不是 Nginx，而是 Caddy：

```bash
systemctl status caddy.service --no-pager -n 40
journalctl -u caddy --since "30 min ago" --no-pager
sed -n '1,220p' /etc/caddy/Caddyfile
```

判断原则：

- `https://www.zhuzhu666.icu/api/health` 正常，说明公网 TLS、Caddy、后端反代基本通。
- Caddy 日志中 `dial tcp 127.0.0.1:8000: connect: connection refused` 通常表示当时 `quant-backend.service` 没有监听或正在重启。
- `nginx -t` 不是当前主入口检查项；除非明确切回 Nginx，否则优先看 Caddy。

## 5.1 数据库先行检查

如果怀疑是数据、任务、归因、外部同步、学习卡片异常，先不要直接翻业务代码，先跑数据库体检：

```bash
cd /home/ubuntu/quant_trading
./.venv/bin/python scripts/db_doctor.py --repair
```

目的：

- 确认 PostgreSQL state 主库和 DuckDB 没有被错误引擎打开
- 确认关键表和关键字段存在
- 自动修复已知历史 schema 漂移

如果 `db_doctor` 不通过，优先修数据库契约，再看策略逻辑。

### Phase H 自主进化链路检查

如果怀疑自治学习、supervisor 学习、自动审批/应用/回滚异常，优先查统一进化账本：

```bash
python - <<'PY'
from backend.core.db import get_state_pg_conn
conn = get_state_pg_conn(read_only=True)
for table in ('evolution_run', 'evolution_decision', 'runtime_config_snapshot'):
    print(f'[{table}]')
    for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1 DESC LIMIT 5"):
        print(dict(row))
conn.close()
PY
```

常用 API：

- `GET /api/learning/evolution/runs`
- `GET /api/learning/evolution/runs/{run_id}`
- `POST /api/learning/position-supervisor/traces/backfill`
- `POST /api/learning/position-supervisor/traces/materialize-labels`

常用验证：

```bash
python scripts/phase_a_health_check.py
python scripts/phase_c_supervisor_check.py --limit 30
```

判断原则：

- `evolution_run` 应能看到样本物化、trace 回填、trace 成熟化、demo 自动治理周期；
- `evolution_decision` 应能看到自动审批、apply switch、rollback 或样本成熟记录；
- `runtime_config_snapshot` 应能看到 startup、parameter template sync、supervisor template switch 等配置版本；
- pending 样本不能直接进入强监督训练，先查 `evidence_contract_json.allowed_uses`。

### Phase H.1 学习 worker 隔离

重训练、自进化、特征工程等高 CPU 任务可以从 `quant-backend.service` 拆到独立 worker，避免和 live API / 交易 loop 抢同一个进程资源。

推荐配置：

```bash
sudo cp /home/ubuntu/quant_trading/deployment/quant-learning-worker.service /etc/systemd/system/quant-learning-worker.service
sudo systemctl daemon-reload
sudo systemctl enable --now quant-learning-worker.service
```

主 backend 侧建议增加环境变量覆盖：

```ini
Environment=QUANT_BACKEND_HEAVY_JOBS=0
Environment=QUANT_BACKEND_LEARNING_SCHEDULERS=0
```

worker 默认使用：

```ini
CPUAffinity=2 3
Environment=QUANT_LEARNING_WORKER_CPU_AFFINITY=2,3
```

这样 worker 可以吃满 2 个核心，主 backend 仍保留数据同步、健康检查、cTrader 和交易接口。

当前 worker / backend 分工：

- `quant-backend.service`: API、WebSocket、cTrader 连接、live loop、轻量健康检查、数据同步入口
- `quant-learning-worker.service`: 学习调度、反事实成熟化、自治学习周期、特征工程、盘外模型重训练
- PostgreSQL `state_v1`: live runtime state 与学习审计主库
- SQLite `data/state.db`: 已删除；不再保留本地冷备，运行态状态只查 PostgreSQL

学习 worker 当前固定使用 `CPUAffinity=2 3`，可以让重训练任务吃满 2 个核心；不要再把高 CPU 学习任务放回 backend 进程。

常用检查：

```bash
systemctl is-active quant-backend.service quant-learning-worker.service
systemctl status quant-backend.service quant-learning-worker.service --no-pager -l
journalctl -u quant-learning-worker.service --since "30 min ago" --no-pager
.venv/bin/python scripts/state_query.py --sql "select status, count(*) from policy_suggestion group by status order by status"
```

学习健康与模型接口：

```text
GET  /api/learning/dataset/quality-health
POST /api/learning/model/open-quality-lightgbm/train
POST /api/learning/model/open-quality-lightgbm/shadow-run
GET  /api/learning/model/open-quality-lightgbm/audits
GET  /api/learning/model/position-quality-lightgbm/audits
GET  /api/learning/model/factor-governance-lightgbm/audits
GET  /api/learning/model/meta-lightgbm/shadow-report
```

判断原则：

- `evidence_contract.bad_total` 应长期为 0；
- 新开仓应带齐 `entry_cluster / bar_context / execution_context / market_micro_context / decision_quality_context / event_context / data_quality_context`；
- 历史开仓缺少实时上下文时保持 degraded 即可，不能伪造；
- `open_quality_lightgbm` 只能 shadow/advisory，不能下单、平仓或改硬风控。

## 6. 改代码前检查

在服务器改代码前，先确认：

```bash
git status --short
git diff --stat
```

如果已经有未提交改动，先判断来源：

- 如果是本轮热修的延续，可以继续
- 如果来源不明，先停下来，不要直接覆盖

## 7. 后端热修流程

标准热修流程：

```text
看日志
  -> 定位问题
  -> 小范围修改
  -> 本地化验证
  -> 看新日志
  -> 必要时重启
  -> 再次验证接口
  -> git diff 审查
  -> 提交
```

### 修改后最少做的事情

```bash
python -m py_compile backend/app.py
```

如果改的是其它文件，就对对应文件做 `py_compile`。

## 8. 服务重启 SOP

### 正常重启

```bash
sudo systemctl restart quant-backend.service
systemctl status quant-backend.service --no-pager -n 30
journalctl -u quant-backend.service --since "2 min ago" --no-pager
```

### 如果服务卡住

先看状态，不要直接乱杀：

```bash
systemctl status quant-backend.service --no-pager
```

只有在确认服务卡死、正常重启无效时，才考虑更强动作。

## 9. 交易循环排查 SOP

如果问题与交易循环有关，默认检查：

1. 登录是否正常
2. `/api/live/loop-status`
3. `/api/live/account`
4. `/api/live/positions`
5. `/api/live/strategy-status`
6. `journalctl` 中最近的 `live loop` / `ctrader` / `gate` / `risk` 日志

### 启停验证

如果需要手动验证交易循环：

- `POST /api/live/start`
- `GET /api/live/loop-status`
- `POST /api/live/stop`

## 10. cTrader 排查 SOP

涉及 cTrader 时重点看：

- `cTrader TCP+TLS connected`
- `App auth OK`
- `Account auth OK`
- `cTrader fully authenticated`
- `auth failed`
- `disconnected`

如果看到的是持续重连风暴，优先判断：

- token / account 是否有效
- bridge 状态是否正确释放
- 后端是否存在旧 loop 或旧连接未退出

### 2026-06-26 新增经验

如果现象是：

- 服务没挂
- 前端显示“不能开仓”或 `warming_up`
- CPU / TCP 连接数同时明显上升

不要只盯登录、鉴权或 loop 开关，优先同时检查下面三件事：

1. `journalctl` 中是否在高频刷 `depth event` / `depth events`
2. 是否存在学习治理接口被频繁访问，导致 `factor_cards / parameter_templates` 重算
3. 已退役的 L2 depth 订阅/writer 或历史独立 collector 是否被误恢复

### 高 CPU 排查 SOP

默认顺序：

```text
看服务 CPU
  -> 看最热线程
  -> 看最近日志热点
  -> 必要时 py-spy 抓 Python 栈
  -> 再决定改哪条热路径
```

推荐命令：

```bash
PID=$(systemctl show -p MainPID --value quant-backend.service)
ps -p $PID -o %cpu,%mem,etime,cmd --no-headers
top -H -b -n 1 -p $PID | head -n 30
sudo /home/ubuntu/quant_trading/.venv/bin/py-spy dump --pid $PID
journalctl _PID=$PID -n 120 --no-pager
```

已确认过的真实根因：

- `execution/ctrader_bridge.py` depth 事件高频日志
- depth 事件逐条同步写 `data/l2.duckdb`
- `/api/learning/*` 导致 `factor_cards.py` / `parameter_templates.py` 重复重算
- 历史独立 L2 collector 与后端主 bridge 同时占用 cTrader Open API 连接

因此在当前现网配置下，如果日志再出现 `depth events` 或 L2 writer 写入，应视为退役链路被误恢复并立即停止。

## 11. 提交前检查

提交前至少执行：

```bash
git status --short
git diff --stat
git diff
```

确认：

- 改动范围符合本次问题
- 没有误改生成文件
- 没有把日志、数据库、缓存带进去

## 12. 禁止事项

服务器操作时默认禁止：

- 不看日志先猜代码
- 不看 `git status` 就直接修改
- 不验证就直接重启多次
- 留下一堆未提交热修
- 用未知来源文件覆盖线上代码
- 在不了解影响时执行破坏性 Git 命令

## 13. 最常用命令速查

```bash
cd /home/ubuntu/quant_trading
git status --short
git rev-parse --short HEAD
systemctl status quant-backend.service --no-pager -n 30
journalctl -u quant-backend.service -n 100 --no-pager
journalctl -u quant-backend.service --since "5 min ago" --no-pager
curl http://127.0.0.1:8000/api/health
codex --version
codex login status
```

## 14. 一句话版本

服务器后端排查的默认顺序是：

```text
先看日志，再看接口，再改代码，最后重启验证。
```

## 15. 启动、依赖与数据速查

后端和测试只使用仓库虚拟环境：

```bash
cd /home/ubuntu/quant_trading
./.venv/bin/python -m backend
./.venv/bin/pytest <targeted-nodeids>
```

`ctrader-open-api==0.9.2` 当前绑定 `protobuf==3.20.1`。升级任一依赖前必须验证 connect/auth、protobuf parse、open/close/reduce 和 SL/TP amend。

PostgreSQL state：

```bash
./.venv/bin/python scripts/state_schema_migrate.py --check
./.venv/bin/python scripts/state_query.py --sql "SELECT key, updated_at FROM runtime_kv ORDER BY updated_at DESC LIMIT 20"
```

schema 写入只能由显式 migration 执行：

```bash
./.venv/bin/python scripts/state_schema_migrate.py --apply
```

### PostgreSQL 灾备（Windows 主动拉取）

当前唯一合同在 `deployment/windows-backup/README.md`：Windows 电脑在线时，经由仅允许 `dump`、备份回执和恢复演练回执的 forced-command SSH key 拉取 `quant_audit` 的完整逻辑快照。服务器不保存备份文件，不启用 `archive_mode`、S3、pgBackRest repository 或 timer；不得把安装了客户端工具或受限 SSH 入口误报为已有可恢复备份。

服务器管理员只安装入口与 Windows 公钥；Windows 拉取后以 `pg_restore --list` 验证文件，并通过回执更新既有 health 投影。只读核对：

```bash
./.venv/bin/python scripts/state_query.py --sql "SELECT value_json, updated_at FROM runtime_kv WHERE key='postgres_backup_health.v1'"
sudo -u postgres psql -Atqc "SHOW archive_mode"
```

恢复只能在隔离 DSN 上执行：`pg_restore` 后运行 `scripts/verify_state_restore.py --confirm-isolated`，它只核对 schema 和记忆完整性，不伪造与在线源的逐行一致性。禁止自动 promote 或切换生产服务；有成功拉取而无成功演练必须保持 `degraded`。

cTrader 常用入口：

```bash
./.venv/bin/python scripts/validate_ctrader_token.py
./.venv/bin/python scripts/backfill_ctrader_deals.py
```

执行价格保持 broker 原值；commission/gross/swap/balance 等 money 字段才按各自 moneyDigits 转换。unknown broker outcome 禁止猜测成功或重发。

Web：

```bash
cd web_frontend
npm test
npm run typecheck
npm run build
```

小程序由 `miniprogram_v2/` 维护，平台行为最终用微信开发者工具验证。任何客户端都只消费 `fact.v1`/canonical snapshot，不重算风控或 readiness。

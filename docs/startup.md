# Quant Trading — 启动指南

> Status: active
> Last verified: 2026-07-06
> Scope: backend, Web frontend, mini-program, and common operational startup commands.
> 当前形态: FastAPI 后端 + Web 完整操作台 + 微信小程序轻量状态面。旧 Vite/Web Console 构建产物不再维护。

> 2026-06-26 工作流更新:
> - 本地 Windows 默认负责前端：`web_frontend`，以及轻量状态面 `miniprogram_v2`
> - 后端、策略、执行、日志排查默认都在 Linux 服务器完成
> - 本文保留本地启动命令，主要用于临时调试，不再作为实盘后端的主工作流

---

## 当前开发启动

Linux 服务器后端必须使用仓库内独立虚拟环境，避免污染 Codex/Hermes 等共享运行环境:

```bash
cd /home/ubuntu/quant_trading
./.venv/bin/python -m pip install --require-hashes -r requirements.txt
./.venv/bin/python -m backend
```

### cTrader / protobuf 依赖边界

当前执行通道使用 `ctrader-open-api==0.9.2`，该 SDK 在 metadata 中固定依赖 `protobuf==3.20.1`。Python 3.12 下这个 protobuf 版本会在测试中触发第三方 `utcfromtimestamp()` 弃用提示，项目通过 `pytest.ini` 精准过滤 `google.protobuf.internal.well_known_types` 里的这条 warning；不要为了消除 warning 单独升级 protobuf。

只有在同时验证下面链路后，才允许升级 `ctrader-open-api` 或 protobuf：

- cTrader connect / auth probe
- protobuf message parse / enum payload
- close / reduce / amend_position_sltp 执行链路

本地 Windows 临时调试时，在仓库根目录启动后端:

```bash
cd C:\Users\zhu\quant_trading
.\.venv\Scripts\python.exe -m backend
```

后端启动前必须配置认证环境变量；缺失时服务会 fail closed:

```env
QUANT_JWT_SECRET=至少 32 字节随机字符串
QUANT_AUTH_USER=登录用户名
QUANT_PASSWORD_HASH=登录密码的 Argon2id 编码摘要
QUANT_AUTH_ALLOW_LEGACY_SHA256=0
QUANT_AUTH_ALLOW_LEGACY_ACCESS_TOKEN=0
QUANT_AUTH_ALLOW_URL_JWT=0
QUANT_AUTH_REVOCATION_STATE_PATH=data/safety/auth_session_revocations.jsonl
```

可在服务器虚拟环境内交互式生成 Argon2id 摘要（命令只读取密码并输出摘要，
不要把明文密码写入 shell history）：

```bash
.venv/bin/python -c "from argon2 import PasswordHasher; import getpass; print(PasswordHasher().hash(getpass.getpass('Password: ')))"
```

旧 SHA-256 密码摘要、旧 access token 和 URL JWT 只允许在客户端迁移窗口内分别
通过上述三个兼容开关显式开启；新部署保持为 `0`。
logout 会在提交 PostgreSQL `auth_session` family 撤销前 fsync 上述本地投影；
该文件必须位于持久磁盘且仅由 backend 用户读写，不能放在会随重启清空的临时目录。
`/api/auth/step-up` 不创建新 refresh session：它只在当前 active `sid/fid` 行内事务更新
`auth_time`，提交成功后签发新的 15 分钟 access token；PostgreSQL 写失败时 start/unlock
继续 fail-closed，stop/emergency 不经过该入口。

默认监听:

- HTTP API: `http://localhost:8000`
- WebSocket: `ws://localhost:8000/ws/state`

等价命令:

```bash
./.venv/bin/uvicorn backend.app:app --host 0.0.0.0 --port 8000
```

需要热重载时:

```bash
./.venv/bin/python -m backend --reload
```

### 学习 / 自治治理 worker

重训练、自主进化、因子自治治理和特征工程默认由独立 worker 承担，避免和 live API / cTrader 交易循环抢 CPU：

```bash
sudo cp /home/ubuntu/quant_trading/deployment/quant-learning-worker.service /etc/systemd/system/quant-learning-worker.service
sudo systemctl daemon-reload
sudo systemctl enable --now quant-learning-worker.service
```

本地调试或一次性验证：

```bash
./.venv/bin/python scripts/learning_worker.py --run-once
```

worker 启动时同样会读取 `settings.yaml` base config，验证 PostgreSQL `runtime_config_overlay` 的 committed mutation/hash authority（历史空 mutation 必须具有完整的 `legacy_authority_json` operator review），恢复通过验证的投影，并写 `learning_worker_startup` snapshot。缺失/悬空 authority 会 fail closed；不得用来源名或 coordinator off 模式绕过。常驻模式会调度 hourly evolution、默认每 15 分钟的 `factor_governance_autonomous`、AWE、feature engineering 和盘外模型任务。

### 持久化研究任务 worker（发布开关默认关闭）

`backtest/discover/tuning/ab_test/external_refresh/sync/factor_health/parameter_template_validation`
八类重任务的 PostgreSQL leased queue 由独立 `quant-job-worker.service` 执行，不属于
backend 或 learning worker。API/学习服务只提交可序列化参数，不把 closure 或 daemon
thread 留在进程内。当前发布配置必须
保持 `QUANT_PG_JOB_QUEUE_V2_ENABLED=0`；安装 unit 不等于允许切换：

```bash
sudo cp /home/ubuntu/quant_trading/deployment/quant-job-worker.service /etc/systemd/system/quant-job-worker.service
sudo systemctl daemon-reload
sudo systemctl disable --now quant-job-worker.service
```

只有在 additive migration、`state_schema_migrate.py --check`、queue 的 PostgreSQL
集成测试、现有 heavy job 排空和受控观察都通过后，才可通过发布级 systemd override
把该静态开关改为 `1`，再启动服务。不要从 RuntimeConfig、治理 overlay 或通用
`/api/config` 热改该开关；回退时先停止 worker，再将开关恢复为 `0`。迁移前遗留
job 固定为 `handler_version=legacy`，不会被新 worker 自动 claim。

静态开关关闭期间的兼容同步任务使用 backend 进程显式所有、最多两个 worker 的
`JobManager` executor；它不借用 asyncio default executor，并由 FastAPI lifespan 在
退出时停止准入、cancel 后 join。持久队列开关开启后，八类重任务的 API 投递不会启动
这个本地 executor。独立 worker 在构造 queue/claim 前会先完整校验
`config/settings.yaml`，再执行只读 state schema 最低版本门禁；任一失败均非零退出，
不会以空默认配置或旧 schema 继续领取任务。

发布时先在 queue=false 前态运行 `pg_job_queue_enable`；切换发布级 flag 并启动 unit
后，再运行 `scripts/phased_repair_release_gate.py --target pg_job_queue_verify`。worker
会把 boot identity、PID、当前状态/任务、八类 handler、进程实际加载的完整静态 flags
写入 `runtime_kv[persistent_job_worker.capability.v1]`。最终门禁要求该心跳不超过 30 秒，
且不以 systemd 单独显示 active 代替进程能力事实。capability 写入失败只使验证
fail-closed，不改变已领取任务的 lease/complete/fail 结果。

---

## 小程序前端

当前已维护的移动端前端是微信小程序 V2，定位为轻量状态面:

```text
C:\Users\zhu\quant_trading\miniprogram_v2
```

用微信开发者工具直接打开该目录。小程序只依赖当前 FastAPI 后端的轻量 live/auth surface:

- `/api/auth/*`
- `/api/live/account`
- `/api/live/positions`
- `/api/live/strategy-status`
- `/api/live/session-stats`
- `/api/live/loop-status`
- `/api/live/realized-pnl-series`
- `/api/risk/summary`
- `/ws/state`

更多说明见 `miniprogram_v2/README.md`。

---

## Web 前端

Web 前端承接完整操作台能力，目录为：

```text
web_frontend/
```

已有源码目录，推荐本地开发与生产调试命令如下：

```bash
cd web_frontend
npm install
npm run dev
```

生产入口由服务器 Caddy 承接：

```text
https://www.zhuzhu666.icu -> Caddy -> FastAPI / Web static
```

当前服务器状态：

- Caddy 监听公网 `:80` / `:443`
- FastAPI 后端只监听 `127.0.0.1:8000`
- `/api/*` 与 `/ws/state` 由 Caddy 反代到 FastAPI

前端职责边界见 [web-frontend-upgrade-plan.md](web-frontend-upgrade-plan.md)。

---

## CLI 模式

```bash
# 回测
python main.py --mode backtest --timeframe M15

# 模拟盘
python main.py --mode paper --timeframe M15 --use-router

# 因子健康评估
python main.py --mode paper --timeframe M15 --factor-health-report

# 因子发现：默认只生成报告，不自动注册
python scripts/discover_factors.py --n-candidates 1000 --top-k 50

# 明确完成 review / 多 forward / 风控门槛后，才允许显式注册 shadow 因子
python scripts/discover_factors.py --n-candidates 1000 --top-k 50 --auto-register
```

---

## cTrader 操作

```bash
# Token 验证
python scripts/validate_ctrader_token.py

# 回填历史成交到 PostgreSQL state_v1.ctrader_deals
python scripts/backfill_ctrader_deals.py --days 30
```

OAuth 脚本是否存在可能随本地凭证流调整；执行前先用 `rg ctrader_oauth scripts` 确认当前入口。

---

## 外部数据刷新

```bash
# 查看各数据源时效
python scripts/refresh_external_data.py --status

# 自动刷新所有过期数据
python scripts/refresh_external_data.py --once

# 强制刷新某个源
python scripts/refresh_external_data.py --source cot --force
```

---

## 数据库体检

2026-06-26 起，数据库层默认先体检再排障。

统一规则:

- `PostgreSQL state_v1` 用于运行时状态；`SQLite` 仅保留 `experiments.db` 和显式临时/迁移源库
- `DuckDB` 只用于行情、外部研究数据、tick、归因、事件库
- 业务代码必须走 `backend/core/db.py` 的统一连接入口

启动前或排障时优先执行:

```bash
# PostgreSQL state schema：默认只读检查，发布时显式 apply
python scripts/state_schema_migrate.py --check
python scripts/state_schema_migrate.py --apply --runner-id release

# canonical experiments.db：默认只读检查，发布时显式 apply
python scripts/experiments_schema_migrate.py --check
python scripts/experiments_schema_migrate.py --apply

# broader 数据库兼容修复 + 体检
python scripts/db_doctor.py --repair
```

backend、learning worker 和模型构造器不会在启动/首次调用时补建 schema；缺对象必须
先由上述 operator migration 修复。非 canonical SQLite 路径只用于隔离 fixture/offline
工具，可保留自初始化行为。

v9 发布在重启 backend/worker 前还必须完成 overlay authority preflight：读取当前
`overlay_hash`、`mutation_id` 和全部顶层 key。非空 mutation 必须能追到
`committed/current` intent 及 matching config/domain hash；历史空 mutation 只能由明确
`operator:*` 身份调用 `RuntimeConfigOverlayService.review_legacy_quarantine()`，并且中央
before/after 分类器必须把每个复核 key 判为 `risk_tightening`。复核绑定精确 hash，部分
复核不会放行；遇到扩张/未知 key 不得伪造 backfill，应保持 no-new-risk，在 typed
Coordinator mutation 下重建或显式清理 overlay 后再受控重启。

这个命令会检查:

- 库文件是否能被正确引擎打开
- 关键表和关键字段是否齐全
- 历史 schema 漂移是否需要自动修复

如果这里不通过，不要先怀疑策略逻辑，先修数据库契约。

---

## 环境要求

- Python 3.11+，推荐 3.12
- cTrader 凭证写入 `.env`
- 微信开发者工具用于 `miniprogram_v2`
- Node.js 用于新 `web_frontend` 开发和浏览器测试；不要用它维护旧 Web Console 构建产物

---

## 常见问题

### 端口冲突

后端默认使用 `:8000`。Windows 上可手动查看并结束占用进程:

```powershell
netstat -ano | findstr :8000
taskkill /F /PID <PID>
```

### WebSocket 离线

先确认后端健康:

```bash
curl http://localhost:8000/api/health
```

再检查小程序配置中的 API base URL 是否指向当前后端。

### cTrader Token 过期

先运行:

```bash
python scripts/validate_ctrader_token.py
```

如果失败，再按当前 `.env` 和凭证流刷新 token。

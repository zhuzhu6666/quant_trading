# Hermes Context — 项目状态快照

> 写给服务器 Hermes：请在你下次新 session 时读这个文件，并用 `memory` 工具保存关键事实。

## Git 版本控制

项目现在使用 Git 管理，本地和服务器通过 `vps` remote 直连同步，GitHub 做备份。

### Remotes

| Remote | URL | 用途 |
|--------|-----|------|
| `origin` | `git@github.com:zhuzhu6666/quant_trading.git` | GitHub 备份 |
| `vps` (仅本地) | `ubuntu@124.221.7.195:/home/ubuntu/quant_trading` | 服务器直传 |

### Git 配置（服务器）

```bash
cd /home/ubuntu/quant_trading
git config user.email 'zhu@quant.local'
git config user.name 'Server Hermes'
git config receive.denyCurrentBranch updateInstead  # 允许本地 push 到当前分支
git config http.proxy http://127.0.0.1:7890          # 推 GitHub 走 clash 代理
git config https.proxy http://127.0.0.1:7890
```

### 工作流

```
本地改小程序 → git push vps main → 服务器同步
服务器改后端 → git commit → git push origin main → GitHub
本地同步后端 → git pull vps main（直连快）或 git pull origin main
```

## 项目架构 (v5)

```
main.py              # CLI 入口 (--mode backtest|paper|live)
├── cli/             # backtest.py, paper.py, live.py
├── core/            # EventBus, AppContext(DI), State, Clock
├── alpha/           # 因子引擎 + GP 发现 + 健康评估
├── strategy/        # MAB Thompson Sampling 路由
├── execution/       # OMS, cTrader bridge, 算法执行
├── risk/            # 四道防线 (前置→熔断→VaR→Kelly)
├── backend/         # FastAPI (端口 8000, systemd: quant-backend)
├── config/          # settings.yaml + RuntimeConfig
├── monitor/         # 告警/自动恢复/系统健康
├── data/            # DuckDB 数据库 (不入 git)
└── tests/           # 38 smoke tests
```

## 微信小程序

- 前端代码在本地: `miniprogram/`
- 服务器上**没有** miniprogram 目录（不在 git 追踪范围内，因为服务器不需要编译小程序）
- 小程序通过 HTTPS API 连服务器: `https://www.zhuzhu666.icu`
- Caddy 反代 `localhost:8000`，自动 SSL + WSS

## API 端点（小程序用）

| 端点 | 方法 | 认证 | 说明 |
|------|------|------|------|
| `/api/auth/login` | POST | 无 | 用户名密码 → JWT |
| `/api/state` | GET | JWT | 全局状态（含 closed_loop 9节点） |
| `/api/live/start` | POST | JWT | 启动管道（body: `{}`，字段有默认值） |
| `/api/live/stop` | POST | 无 | 停止管道 |
| `/api/v4/stats` | GET | JWT | 归因统计（status=ok/no_data） |
| `/api/v4/weights` | GET | JWT | 因子权重（字段: factor, new） |
| `/api/system/db-health` | GET | 无 | 数据库健康（8库） |
| `/api/control/evolution/latest` | GET | JWT | 进化事件 |

## 服务器运行时

- 后端: `sudo systemctl restart quant-backend`
- Python: `/home/ubuntu/quant_trading/.venv/bin/python`
- 代理: clash (mihomo), mixed-port 7890, external-controller 127.0.0.1:9090
- 域名: `www.zhuzhu666.icu` → Caddy → `localhost:8000`
- 环境变量: `/home/ubuntu/quant_trading/.env` (含 QUANT_JWT_SECRET)

## 当前状态

- 管道: 已停止
- cTrader: 周末无连接（demo 服务器周末不下发 auth）
- 数据: ticks.duckdb (6.7G), ctrader_data.duckdb (127M), l2.duckdb (21M)
- 测试: 38 个 test，`python -m pytest tests/ -v` 运行

## 你的职责

你是**后端 Hermes**，负责：
1. 审查/修复/重构后端代码
2. 跑 pytest 验证
3. 改完后 `git commit && git push origin main`
4. 改动后通知用户（以便本地同步）

用户本地 Hermes 负责前端（小程序）。

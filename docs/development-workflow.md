# Development Workflow

> Last updated: 2026-06-26
> Scope: local mini-program frontend + Linux server backend workflow.

本文用来固化当前项目的唯一推荐协作方式，目标只有一个：
避免 Windows 本地副本和 Linux 服务器运行代码长期分叉。

## 1. 核心原则

1. 本地 Windows 只负责微信小程序前端。
2. Linux 服务器是后端、策略、执行、配置、日志的唯一真实工作区。
3. 实盘相关代码不再采用“本地修改后再手工同步服务器”的方式。
4. 后端问题以服务器复现、服务器修改、服务器验证为准。
5. 前端问题以本地微信开发者工具验证为准。

## 2. 角色边界

### 本地 Windows

本地只负责这些内容：

- `miniprogram_v2`
- 前端交互、展示、登录态跳转
- 小程序页面验证
- 文档整理

本地默认不再承担这些工作：

- `backend`
- `execution`
- `alpha`
- `risk`
- `monitor`
- `.env` / systemd / cTrader / 数据库 / 实盘日志

### Linux 服务器

服务器负责这些内容：

- 后端接口
- 交易循环
- 风控逻辑
- cTrader 连接与执行
- 数据库、日志、环境变量、systemd
- 所有实盘相关验证

### Git

Git 仍然保留，但用途要更明确：

- 前端改动：本地提交
- 后端改动：服务器提交
- 不再要求“后端先在本地改，再推服务器”

## 3. 目录约定

### 本地主要目录

```text
miniprogram_v2/
docs/
TODO.md
```

### 服务器主要目录

```text
backend/
execution/
alpha/
risk/
monitor/
config/
data/
logs/
scripts/
tests/
```

## 4. 标准工作流

### 前端开发流程

```text
本地修改 miniprogram_v2
  -> 微信开发者工具验证
  -> 如接口异常，再去服务器排查后端
  -> 前端确认稳定后提交
```

### 后端开发流程

```text
SSH 到服务器
  -> 在服务器上改代码
  -> 在服务器上跑最小验证
  -> 看日志
  -> 必要时重启服务
  -> 再做接口验证
  -> 确认后提交
```

### 实盘问题排查流程

```text
先看服务器日志
  -> 再看接口状态
  -> 再看数据库 / 配置 / 运行态
  -> 最后才改代码
```

## 5. 服务器日常操作规范

### 登录服务器

当前服务器信息：

- Host: `124.221.7.195`
- User: `ubuntu`
- 项目目录: `/home/ubuntu/quant_trading`

### 常用命令

```bash
cd /home/ubuntu/quant_trading
git status --short
git rev-parse HEAD
systemctl status quant-backend.service --no-pager
journalctl -u quant-backend.service -n 100 --no-pager
curl http://127.0.0.1:8000/api/health
```

### 服务重启

```bash
sudo systemctl restart quant-backend.service
systemctl status quant-backend.service --no-pager -n 30
journalctl -u quant-backend.service --since "2 min ago" --no-pager
```

### 最小验证

每次后端改动后，至少验证：

```bash
curl http://127.0.0.1:8000/api/health
```

如果改动涉及登录或交易页，再额外验证：

```bash
curl -X POST http://127.0.0.1:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"zhu","password":"1994"}'
```

如涉及实盘接口，再继续验证：

- `/api/live/account`
- `/api/live/positions`
- `/api/live/strategy-status`
- `/api/live/loop-status`
- `/api/risk/summary`

## 6. Codex CLI 使用规则

服务器已经安装并登录 `codex`，当前用法如下：

```bash
codex
codex --version
codex login status
```

当前约定：

- 本地 Codex：负责前端、小程序、文档、总控协作
- 服务器 Codex：负责后端、实盘、日志、配置、热修

服务器 Codex 适合：

- 服务器才复现的问题
- 交易循环、风控、cTrader 排查
- systemd / `.env` / 数据库 / 权限问题
- 小范围后端改动和验证

服务器 Codex 不适合：

- 改小程序页面
- 本地 UI 联调
- 长期在服务器堆积未整理改动

## 7. 提交规则

### 前端提交

前端改动在本地提交。

示例：

```bash
git add miniprogram_v2
git commit -m "fix mini program trading status rendering"
```

### 后端提交

后端改动优先在服务器提交。

示例：

```bash
cd /home/ubuntu/quant_trading
git add backend execution alpha risk monitor config tests
git commit -m "fix live loop factor restore path"
```

## 8. 明确禁止的做法

以下做法从现在开始默认禁止：

- 在本地修改后端代码，再手工 `scp` 整批覆盖服务器
- 不确认服务器当前代码状态，就直接从本地覆盖
- 后端同时在本地和服务器两边并行修改
- 服务器保留长期未提交热修
- 看到异常先猜代码，再看日志

补充说明：

- 紧急线上止血时，允许做“小文件、单问题、可回滚”的临时同步，但必须满足：
  - 先记录为什么没直接在服务器工作区改
  - 只同步本次热修涉及的单个或少量文件
  - 同步后立刻补文档、补验证结果、补后续服务器工作区收口动作
- 这种临时同步只能算例外，不能重新退回“本地改后端、服务器只接收覆盖”的旧模式

## 9. 每次后端改动后的检查清单

```text
[ ] 改动发生在服务器
[ ] 已查看 git diff
[ ] 已做最小接口验证
[ ] 已查看最新 journalctl
[ ] 如涉及交易循环，已验证 start / stop 或相关状态接口
[ ] 服务状态正常
[ ] 改动已提交或明确记录原因

如果这次改动牵涉：

- cTrader 连接
- `execution/ctrader_bridge.py`
- `backend/services/live_service.py`
- `/api/learning/*`
- `DuckDB` / `SQLite` 路径

则额外检查：

```text
[ ] 已确认 CPU 没有持续爬升
[ ] 已确认 TCP 连接数没有持续异常增长
[ ] 已确认 /api/live/status 最终回到 connected / ready
[ ] 已确认没有把当前不需要的 L2 depth 默认挂回 live 主链
```
```

## 10. 一句话版本

当前项目从现在开始按这条规则执行：

```text
本地只做小程序，服务器只做后端。
```

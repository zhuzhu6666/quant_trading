# Server Backend SOP

> Last updated: 2026-06-26
> Scope: Linux server backend daily operations.

这份文档只服务一个目标：
把服务器上的后端日常操作标准化，减少临场判断和误操作。

## 1. 适用范围

这份 SOP 适用于：

- 后端接口排查
- 交易循环排查
- cTrader 连接问题
- `.env` / systemd / 日志 / 数据库问题
- 服务器热修

这份 SOP 不适用于：

- 小程序页面开发
- 本地 UI 调整
- 微信开发者工具操作

## 2. 基础信息

当前服务器：

- IP: `124.221.7.195`
- SSH User: `ubuntu`
- Project Root: `/home/ubuntu/quant_trading`
- Service: `quant-backend.service`

## 3. 登录后第一步

SSH 进入服务器后，先执行：

```bash
cd /home/ubuntu/quant_trading
pwd
git status --short
git rev-parse --short HEAD
systemctl is-active quant-backend.service
```

目的：

- 确认当前目录正确
- 确认工作区是否脏
- 确认当前代码版本
- 确认后端服务是否还活着

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
```

### 登录检查

```bash
curl -X POST http://127.0.0.1:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"zhu","password":"1994"}'
```

### 常用业务接口

```bash
curl http://127.0.0.1:8000/api/live/loop-status
curl http://127.0.0.1:8000/api/health
```

如果需要带 JWT，就先登录再带 `Authorization: Bearer ...` 调用：

- `/api/live/account`
- `/api/live/positions`
- `/api/live/strategy-status`
- `/api/live/session-stats`
- `/api/risk/summary`

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

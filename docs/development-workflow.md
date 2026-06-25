# Development Workflow

> Last updated: 2026-06-25
> Scope: local, GitHub, and server synchronization rules.

本文固化三端协作流程，避免本地、GitHub、服务器出现长期分叉。

## 1. 三端角色

### 本地

本地是主开发和编排端。

适合：

- Codex 主控开发。
- 架构设计和文档整理。
- 小程序前端 `miniprogram_v2` 开发和验证。
- 可在本地复现的后端改动。
- 提交、推送、发布协调。

### GitHub

GitHub `main` 是唯一最终合并源。

所有端最终都必须回到 GitHub commit：

```text
local / server changes
  -> commit
  -> push GitHub
  -> pull --ff-only to other endpoints
```

### 服务器

服务器是后端真实运行和验证端。

适合：

- 后端运行态验证。
- `.env`、systemd、真实数据、cTrader 连接排查。
- 服务器日志、数据库、定时任务检查。
- 必要的短事务热修。

服务器不适合作为长期第二开发分支。服务器如需改代码，必须短事务完成并立即回推 GitHub。

## 2. 标准开发流程

日常优先使用本流程：

```text
1. 本地开发
2. 本地测试
3. git commit
4. git push origin main
5. SSH 到服务器
6. git pull --ff-only origin main
7. 服务器测试
8. 必要时重启服务
9. 校验本地 / GitHub / 服务器 HEAD 一致
```

常用命令：

```bash
git status --short
python -m pytest tests\research\test_rule_learning_pipeline.py tests\research\test_model_registry.py -q
git add -A
git commit -m "..."
git push origin main
```

服务器：

```bash
cd /home/ubuntu/quant_trading
git pull --ff-only origin main
.venv/bin/python -m pytest tests/research/test_rule_learning_pipeline.py tests/research/test_model_registry.py -q
.venv/bin/python scripts/phase_b_risk_check.py --api-base https://YOUR_HOST --username YOUR_USER --password YOUR_PASSWORD
.venv/bin/python scripts/phase_b_risk_check.py --api-base https://YOUR_HOST --username YOUR_USER --password YOUR_PASSWORD --position-id 123456789
git status --short
git rev-parse HEAD
```

三端校验：

```bash
git rev-parse HEAD
git ls-remote origin refs/heads/main
ssh ubuntu@SERVER "cd /home/ubuntu/quant_trading && git rev-parse HEAD && git status --short"
```

## 3. 后端代码规则

后端真实环境以服务器验证为准，但代码仍以 GitHub 为最终合并源。

推荐方式：

```text
本地改后端
  -> 本地测试
  -> push GitHub
  -> 服务器 pull
  -> 服务器 .venv 测试
  -> 重启服务 / 验证接口
```

如果问题只在服务器复现，可以在服务器短事务热修：

```text
服务器定位问题
  -> 小范围修改
  -> 服务器测试
  -> git diff 审查
  -> commit
  -> push GitHub
  -> 本地 pull --ff-only
  -> 三端 HEAD 校验
```

服务器热修禁止长期保留未提交改动。

## 4. 前端代码规则

前端以本地 `miniprogram_v2` 开发和验证为准。

推荐方式：

```text
本地小程序开发
  -> 微信 DevTools 验证
  -> commit
  -> push GitHub
  -> 服务器 pull
```

服务器不作为前端开发端，只同步最终代码。

## 5. Codex CLI 使用建议

当前推荐：

- 本地 Codex 是主控。
- 服务器可以安装 Codex CLI，但只作为现场排障和短事务热修工具。

服务器 Codex CLI 适合：

- 服务器才复现的后端错误。
- systemd / 环境变量 / 权限 / 依赖排查。
- 查看真实日志和数据库状态。
- 小范围热修并立即提交。

服务器 Codex CLI 不适合：

- 和本地 Codex 并行做大改。
- 长期保留未提交后端改动。
- 直接绕过 GitHub 形成服务器专属版本。

## 6. 同步安全规则

- 默认使用 `git pull --ff-only`，避免服务器产生隐式 merge。
- 发布前必须 `git status --short`。
- 发布后必须校验三端 HEAD。
- 服务器有本地改动时，先判断来源；不能直接覆盖未知改动。
- 生成报告、缓存、数据库、日志不应进入提交。
- 如果服务器存在旧生成文件挡住 pull，先备份或 stash，再同步。

## 7. 发布检查清单

每次发布至少检查：

```text
[ ] 本地测试通过
[ ] 本地工作区干净
[ ] GitHub push 成功
[ ] 服务器 pull --ff-only 成功
[ ] 服务器核心测试通过
[ ] Phase B 风控摘要接口验证通过（`/api/risk/summary`、`/api/risk/policy/verdicts`）
[ ] 必要服务已重启
[ ] 本地 / GitHub / 服务器 HEAD 一致
[ ] 服务器工作区干净
```

## 8. 冲突处理

如果服务器有未提交代码：

```bash
git status --short
git diff
```

然后二选一：

- 是有效服务器热修：提交并 push GitHub，再本地 pull。
- 是生成物或旧报告：备份或 stash，再从 GitHub fast-forward。

禁止在不了解改动来源时执行破坏性 reset。

## 9. 原则

1. GitHub 是最终真相源。
2. 本地是主开发端。
3. 服务器是运行和验证端。
4. 服务器可以热修，但必须短事务回推。
5. 前端本地验证，后端服务器验证。
6. 每轮开发结束必须三端 HEAD 一致。

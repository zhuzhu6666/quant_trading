# MemPalace 自动保存钩子

## 钩子清单

| 钩子 | 触发 | 做什么 |
|------|------|--------|
| `pre-compact.sh` | Claude Code 准备压缩 context 前 | sweep 当前会话 JSONL 入 `claude-sessions` wing |
| `periodic-save.sh` | Windows schtasks 每 30 分钟 | 增量 sweep `~/.claude/projects/` 下所有 JSONL |
| `run-periodic-save.bat` | schtasks 包装器 | cd 到项目根 + 设 `CLAUDE_PROJECT_DIR` + 调 bash 跑 `periodic-save.sh` |

## 守卫逻辑

两个 bash 钩子首行都检查 `CLAUDE_PROJECT_DIR` 是否等于本项目路径。
切到其他项目时,钩子**立即退出 0**,不污染其他项目的宫殿。

`run-periodic-save.bat` 替 schtasks 子进程**自动设** `CLAUDE_PROJECT_DIR`,因为 schtasks 不会注入这个变量。

## 时间戳问题(已修)

第一次 schtasks 触发时,`date` 命令在 schtasks 启的 bash PATH 里找不到,log 显示空时间戳。改用 bash 内建 `printf '%(%Y-%m-%dT%H:%M:%S%z)T' -1`,不依赖外部命令。

## 守卫验证

```bash
# 在别的目录跑,应秒退
cd /tmp
CLAUDE_PROJECT_DIR="C:\\other" bash C:\Users\zhu\quant_trading\.claude\hooks\periodic-save.sh --dry-run
echo $?  # 期望 0,无输出
```

## 日志位置

- `~/.mempalace/logs/pre-compact.log`
- `~/.mempalace/logs/periodic-save.log`

`.gitignore` 已加 `.claude/hooks/*.log` 防止误 commit。

## wing 关联

钩子 sweep 时会 `cd` 到 `C:\Users\zhu\.mempalace\sessions` 目录,因为 `mempalace sweep <target>` **只接受一个位置参数**,**不接 `--wing` flag**——wing 由 CWD 决定。

## 卸载

```bash
# 1. 卸计划任务
schtasks /delete /tn "MempalacePeriodicSave" /f

# 2. 卸 MCP
claude mcp remove "mempalace" -s local

# 3. 删钩子
rm -rf C:\Users\zhu\quant_trading\.claude\hooks

# 4. 改 .claude.json
#    删 projects.C:/Users/zhu/quant_trading.mcpServers.mempalace 段
```

完整卸载见 `docs/superpowers/specs/2026-06-07-mempalace-install-design.md` §9。

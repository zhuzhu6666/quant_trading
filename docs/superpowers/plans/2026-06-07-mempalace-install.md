# MemPalace 全套安装实施 plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Windows 11 / Python 3.12 / 无 Docker 环境下,把 MemPalace v3.4.x 装上,配 ChromaDB 后端 + embeddinggemma-300m,绑 quant_trading 项目,接 MCP 暴露 5 个工具,挂两个条件钩子(PreCompact + schtasks 定期),全程数据存 `~/.mempalace/`。

**Architecture:** 1 个 spec 拆成 7 个原子任务,每个任务独立可回滚。MCP server 走 `uv tool install` 落在 PATH,Claude Code 直接 spawn,无需 Docker。钩子放项目根 `.claude/hooks/`,首行守卫 `CLAUDE_PROJECT_DIR` 防跨项目污染。会话 wing 不挂 MCP,只 CLI 用。

**Tech Stack:** Python 3.12, uv 0.11.19, MemPalace v3.4.0+, ChromaDB (默认后端), embeddinggemma-300m, bash 钩子, Windows schtasks 计划任务, Git (频繁提交)。

**Spec:** `docs/superpowers/specs/2026-06-07-mempalace-install-design.md` (commit `0bfcc92`)

---

## 文件结构 (本 plan 涉及)

| 路径 | 状态 | 职责 |
|------|------|------|
| `~/.mempalace/projects/` | 新建(全局) | 项目代码 wing 根 |
| `~/.mempalace/projects/config.json` | 新建 | wing: quant-trading 配置 |
| `~/.mempalace/projects/backends.json` | 新建 | ChromaDB 标记 |
| `~/.mempalace/sessions/` | 新建(全局) | 会话 wing 根 |
| `~/.mempalace/sessions/config.json` | 新建 | wing: claude-sessions 配置 |
| `~/.mempalace/logs/` | 新建 | 钩子日志目录 |
| `C:\Users\zhu\quant_trading\.claude\hooks\pre-compact.sh` | 新建 | PreCompact 钩子 |
| `C:\Users\zhu\quant_trading\.claude\hooks\periodic-save.sh` | 新建 | 定期 sweep 钩子 |
| `C:\Users\zhu\quant_trading\.claude\settings.local.json` | 新建或修改 | MCP + hooks 注册 |
| `C:\Users\zhu\quant_trading\.gitignore` | 修改 | 忽略 `.claude/hooks/*.log` |
| `docs/superpowers/install-logs/` | 新建 | 本 plan 执行日志存档 |

---

## Task 1: 装 CLI + 验证安装

**Files:** 无 (安装到 PATH)

- [ ] **Step 1: 装前环境检查**

```bash
uv --version
python --version
where python
# 期望: uv 0.11+, Python 3.9+, 路径显示 Python 3.12
```

```bash
df -h ~ | tail -1
# 期望: 可用空间 >= 1GB (300MB 模型 + chroma 数据)
```

- [ ] **Step 2: 安装 MemPalace**

```bash
uv tool install mempalace
# 期望: Installed mempalace-X.Y.Z ... 3-4 秒
```

- [ ] **Step 3: 验证可执行**

```bash
mempalace --version
# 期望: mempalace, version 3.4.0 (或更新)

where mempalace
# 期望 (Windows):
# C:\Users\zhu\.local\bin\mempalace.exe
# C:\Users\zhu\.local\bin\mempalace
```

- [ ] **Step 4: 失败回滚测试 (dry)**

```bash
uv tool uninstall mempalace --dry-run
# 期望: Would remove mempalace (确认能干净卸)
```

**NOTE**: 不要真卸,只是确认回滚路径存在。

- [ ] **Step 5: Commit (空 install 文档)**

无代码改动,但记一下 milestone:

```bash
git checkout -b chore/mempalace-install
echo "## MemPalace install log

- 2026-06-07: Task 1 完成 — CLI 已装 ($(mempalace --version))
- 路径: $(where mempalace | head -1)
" > docs/superpowers/install-logs/task-01-cli-install.md
git add docs/superpowers/install-logs/task-01-cli-install.md
git commit -m "chore(mempalace): task 01 - CLI installed"
```

---

## Task 2: 初始化两个 wing(宫殿)

**Files:**
- Create: `~/.mempalace/projects/config.json`
- Create: `~/.mempalace/projects/backends.json`
- Create: `~/.mempalace/sessions/config.json`
- Create: `~/.mempalace/sessions/backends.json`
- Create: `~/.mempalace/logs/.gitkeep`

- [ ] **Step 1: 建目录树**

```bash
mkdir -p ~/.mempalace/projects/chroma
mkdir -p ~/.mempalace/projects/embed_cache
mkdir -p ~/.mempalace/sessions/chroma
mkdir -p ~/.mempalace/sessions/embed_cache
mkdir -p ~/.mempalace/logs
```

- [ ] **Step 2: 初始化项目 wing**

```bash
mempalace init ~/.mempalace/projects --backend chromadb --model embeddinggemma-300m
# 期望: Initialized palace at ~/.mempalace/projects ...
#       Backend: chromadb
#       Model: embeddinggemma-300m
```

- [ ] **Step 3: 初始化会话 wing**

```bash
mempalace init ~/.mempalace/sessions --backend chromadb --model embeddinggemma-300m
# 期望: 同上,路径换 sessions
```

- [ ] **Step 4: 验证两个 wing 配置**

```bash
cat ~/.mempalace/projects/config.json
# 期望: JSON 含 backend=chromadb, model=embeddinggemma-300m, wing=quant-trading

cat ~/.mempalace/sessions/config.json
# 期望: 同上,wing=claude-sessions
```

```bash
ls ~/.mempalace/projects/chroma/
# 期望: chroma.sqlite3 等文件存在

ls ~/.mempalace/sessions/chroma/
# 期望: 同上
```

- [ ] **Step 5: 修 wing 名(spec 要求显式命名)**

如果 `config.json` 里 wing 名是 `default` 或未设,手动改:

```bash
# 读出现有 config
cat ~/.mempalace/projects/config.json
```

若 wing 字段缺失或不正确,用 `Edit` 工具修改:
- `~/.mempalace/projects/config.json` → `"wing": "quant-trading"`
- `~/.mempalace/sessions/config.json` → `"wing": "claude-sessions"`

- [ ] **Step 6: 创建 backends.json 标记文件**

```bash
echo '{
  "backend": "chromadb",
  "namespace_isolation": false,
  "warning": "All data local. Do not point this to external chromadb without explicit opt-in."
}' > ~/.mempalace/projects/backends.json

echo '{
  "backend": "chromadb",
  "namespace_isolation": false,
  "warning": "All data local. Do not point this to external chromadb without explicit opt-in."
}' > ~/.mempalace/sessions/backends.json
```

- [ ] **Step 7: 创建日志目录占位**

```bash
touch ~/.mempalace/logs/.gitkeep
```

- [ ] **Step 8: Commit**

```bash
git checkout -b chore/mempalace-init
# 全局目录不进 git,但记 milestone
echo "## Task 2: 两个 wing 已初始化

- projects wing: quant-trading (chromadb + embeddinggemma-300m)
- sessions wing: claude-sessions (chromadb + embeddinggemma-300m)
- 数据根: ~/.mempalace/
- 磁盘占用: $(du -sh ~/.mempalace/) (含 300MB embeddinggemma 模型)
" > docs/superpowers/install-logs/task-02-palace-init.md
git add docs/superpowers/install-logs/task-02-palace-init.md
git commit -m "chore(mempalace): task 02 - two wings initialized"
```

---

## Task 3: Mine quant_trading 项目代码

**Files:** 无 (只往 ChromaDB 写,不写项目文件)

- [ ] **Step 1: 干跑看范围**

```bash
mempalace mine C:\Users\zhu\quant_trading --wing quant-trading --mode files --dry-run
# 期望: 报告预计入库 N 个文件 (N >= 50)
# 注意 Room 会按子目录自动推:code/docs/scripts/root
```

- [ ] **Step 2: 用户确认时间窗口**

**关键决策点**:首次 mine 预计 5-15 分钟。问用户:
- **前台跑**: 阻塞当前会话 5-15 分钟,但你能实时看到进度
- **后台跑**: 用 `run_in_background: true`,不阻塞,装完会自动结束

默认推荐**前台**——更稳,出问题能立刻停。

如果用户选后台,在 Step 3 用 `run_in_background: true` 调用 `mempalace mine ...`,然后 Step 4 改成 `BashOutput` 轮询。

- [ ] **Step 3: 真跑 mine(全量)**

```bash
mempalace mine C:\Users\zhu\quant_trading --wing quant-trading --mode files
# 期望输出最后一行: "Mined N files into wing 'quant-trading' (X.X MB)"
# 耗时: 5-15 分钟
```

- [ ] **Step 4: 验证入库**

```bash
mempalace search "XAUUSD M15 趋势" --wing quant-trading --limit 3
# 期望: 至少 1 条 drawer,内容含 "XAUUSD" 或 "M15" 相关 verbatim
```

```bash
mempalace search "回测 Sharpe" --wing quant-trading --limit 3
# 期望: 至少 1 条 drawer,内容是 strategy/ 或 docs/ 里的相关片段
```

- [ ] **Step 5: 列出 wing 内 room 分布**

```bash
mempalace list-rooms --wing quant-trading
# 期望: code / docs / scripts / root 四个 room,各显示文件数
```

- [ ] **Step 6: 失败处理**

如果 Step 3 中途失败:
```bash
# 1. 报 stderr 给用户
# 2. 不重试,问用户:继续(可能部分文件丢) / 整库删了重来 / 停
# 3. 如果用户选重来:
rm -rf ~/.mempalace/projects/chroma/*
mempalace init ~/.mempalace/projects --backend chromadb --model embeddinggemma-300m
# 然后回到 Step 1 重跑
```

- [ ] **Step 7: Commit milestone**

```bash
git checkout -b chore/mempalace-mine-project
echo "## Task 3: quant_trading 项目已 mine

- 模式: --mode files
- 干跑报告: 见 install logs
- 实际入库: $(mempalace list-rooms --wing quant-trading --json | jq '.rooms | map({room, count})')
- search 验证:
  - 'XAUUSD M15 趋势' → $(mempalace search 'XAUUSD M15 趋势' --wing quant-trading --limit 1 --json | jq '.results | length') 条
  - '回测 Sharpe' → $(mempalace search '回测 Sharpe' --wing quant-trading --limit 1 --json | jq '.results | length') 条
" > docs/superpowers/install-logs/task-03-mine-project.md
git add docs/superpowers/install-logs/task-03-mine-project.md
git commit -m "chore(mempalace): task 03 - quant_trading project mined"
```

---

## Task 4: Mine Claude 会话历史

**Files:** 无 (只往 ChromaDB 写)

- [ ] **Step 1: 干跑看范围**

```bash
mempalace mine C:\Users\zhu\.claude\projects --wing claude-sessions --mode convos --dry-run
# 期望: 报告预计入库 N 个会话 (可能 1-10 个,看历史多久)
```

- [ ] **Step 2: 真跑 mine**

```bash
mempalace mine C:\Users\zhu\.claude\projects --wing claude-sessions --mode convos
# 期望: "Mined N sessions into wing 'claude-sessions'"
# 比项目 mine 快(会话文件数 << 代码文件数)
```

- [ ] **Step 3: 验证**

```bash
mempalace search "ECC 闪烁" --wing claude-sessions --limit 3
# 期望: 命中 6-04 闪烁事故那次会话的内容
```

```bash
mempalace search "dmPolicy pairing" --wing claude-sessions --limit 3
# 期望: 命中 tg-access-policy-pairing-trap 讨论
```

- [ ] **Step 4: Commit**

```bash
git checkout -b chore/mempalace-mine-sessions
echo "## Task 4: Claude 会话已 mine

- 模式: --mode convos
- 入库会话数: $(mempalace list-rooms --wing claude-sessions --json | jq '.rooms | map({room, count})')
- search 验证:
  - 'ECC 闪烁' → $(mempalace search 'ECC 闪烁' --wing claude-sessions --limit 1 --json | jq '.results | length') 条
  - 'dmPolicy pairing' → $(mempalace search 'dmPolicy pairing' --wing claude-sessions --limit 1 --json | jq '.results | length') 条
" > docs/superpowers/install-logs/task-04-mine-sessions.md
git add docs/superpowers/install-logs/task-04-mine-sessions.md
git commit -m "chore(mempalace): task 04 - claude sessions mined"
```

---

## Task 5: 写两个钩子脚本

**Files:**
- Create: `C:\Users\zhu\quant_trading\.claude\hooks\pre-compact.sh`
- Create: `C:\Users\zhu\quant_trading\.claude\hooks\periodic-save.sh`

- [ ] **Step 1: 创建 hooks 目录**

```bash
mkdir -p C:\Users\zhu\quant_trading\.claude\hooks
```

- [ ] **Step 2: 写 pre-compact.sh**

文件 `C:\Users\zhu\quant_trading\.claude\hooks\pre-compact.sh`,完整内容:

```bash
#!/usr/bin/env bash
# PreCompact hook: 压缩 context 前自动 sweep 当前会话入库
# 守卫: 只在 quant_trading 项目下激活

set -u

# === 守卫: 跨项目秒退 ===
EXPECTED_PROJECT="C:\\Users\\zhu\\quant_trading"
ACTUAL_PROJECT="${CLAUDE_PROJECT_DIR:-}"
if [ "$ACTUAL_PROJECT" != "$EXPECTED_PROJECT" ]; then
    # 守卫触发,绝不算错
    exit 0
fi

# === 读 stdin (Claude Code 注入 {transcript_path, session_id}) ===
TRANSCRIPT_PATH=""
if [ ! -t 0 ]; then
    STDIN_JSON=$(cat 2>/dev/null || true)
    if [ -n "$STDIN_JSON" ]; then
        TRANSCRIPT_PATH=$(echo "$STDIN_JSON" | grep -oE '"transcript_path"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"transcript_path"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/' || true)
    fi
fi

# === 决定 sweep 目录 ===
SESSIONS_ROOT="C:\\Users\\zhu\\.claude\\projects\\C--Users-zhu-quant-trading"
if [ ! -d "$SESSIONS_ROOT" ]; then
    # 目录不存在,静默失败
    exit 0
fi

# === 跑 sweep,失败不阻塞 ===
LOG_FILE="C:\\Users\\zhu\\.mempalace\\logs\\pre-compact.log"
mkdir -p "$(dirname "$LOG_FILE")"

{
    echo "[$(date -Iseconds 2>/dev/null || date)] PreCompact sweep start"
    echo "  transcript_path: $TRANSCRIPT_PATH"
    echo "  sessions_root: $SESSIONS_ROOT"

    mempalace sweep "$SESSIONS_ROOT" --wing claude-sessions --force 2>&1 || echo "  sweep exited non-zero, ignoring"

    echo "[$(date -Iseconds 2>/dev/null || date)] PreCompact sweep done"
} >> "$LOG_FILE" 2>&1

# 永远 0
exit 0
```

- [ ] **Step 3: 写 periodic-save.sh**

文件 `C:\Users\zhu\quant_trading\.claude\hooks\periodic-save.sh`,完整内容:

```bash
#!/usr/bin/env bash
# Periodic save hook: 由 schtasks 每 30 分钟触发
# 守卫: 只在 quant_trading 项目下激活

set -u

# === 守卫: 跨项目秒退 ===
EXPECTED_PROJECT="C:\\Users\\zhu\\quant_trading"
ACTUAL_PROJECT="${CLAUDE_PROJECT_DIR:-$(pwd)}"
if [ "$ACTUAL_PROJECT" != "$EXPECTED_PROJECT" ]; then
    # 守卫触发,绝不算错
    exit 0
fi

# === dry-run 模式 (验收用) ===
if [ "${1:-}" = "--dry-run" ]; then
    SESSIONS_ROOT="C:\\Users\\zhu\\.claude\\projects\\C--Users-zhu-quant-trading"
    if [ -d "$SESSIONS_ROOT" ]; then
        N=$(find "$SESSIONS_ROOT" -name "*.jsonl" 2>/dev/null | wc -l)
        echo "[dry-run] would sweep $N jsonl files in $SESSIONS_ROOT"
    else
        echo "[dry-run] sessions dir not found: $SESSIONS_ROOT"
    fi
    exit 0
fi

# === 跑 sweep,失败不阻塞 ===
SESSIONS_ROOT="C:\\Users\\zhu\\.claude\\projects\\C--Users-zhu-quant-trading"
LOG_FILE="C:\\Users\\zhu\\.mempalace\\logs\\periodic-save.log"
mkdir -p "$(dirname "$LOG_FILE")"

if [ ! -d "$SESSIONS_ROOT" ]; then
    # 没会话目录,静默
    exit 0
fi

{
    echo "[$(date -Iseconds 2>/dev/null || date)] Periodic sweep start"
    mempalace sweep "$SESSIONS_ROOT" --wing claude-sessions 2>&1 || echo "  sweep exited non-zero, ignoring"
    echo "[$(date -Iseconds 2>/dev/null || date)] Periodic sweep done"
} >> "$LOG_FILE" 2>&1

exit 0
```

- [ ] **Step 4: 设置可执行权限**

```bash
chmod +x C:\Users\zhu\quant_trading\.claude\hooks\pre-compact.sh
chmod +x C:\Users\zhu\quant_trading\.claude\hooks\periodic-save.sh

ls -la C:\Users\zhu\quant_trading\.claude\hooks\
# 期望: 两个 .sh 文件均有 -rwxr-xr-x
```

- [ ] **Step 5: 干跑验收**

```bash
# pre-compact 干跑 (用 fake event)
echo '{"transcript_path":"C:/tmp/fake.jsonl","session_id":"test"}' | bash C:\Users\zhu\quant_trading\.claude\hooks\pre-compact.sh
echo "exit: $?"
# 期望: exit: 0, log 写入 ~/.mempalace/logs/pre-compact.log

# periodic-save 干跑
bash C:\Users\zhu\quant_trading\.claude\hooks\periodic-save.sh --dry-run
echo "exit: $?"
# 期望: exit: 0, 输出 "[dry-run] would sweep N jsonl files"
```

- [ ] **Step 6: 守卫验证(切到别的项目,钩子秒退)**

```bash
cd /tmp
CLAUDE_PROJECT_DIR="C:\\Users\\zhu\\other" bash C:\Users\zhu\quant_trading\.claude\hooks\pre-compact.sh
echo "exit: $?"
# 期望: exit: 0, 立即返回(无 sweep 调用)
```

- [ ] **Step 7: Commit**

```bash
git checkout -b chore/mempalace-hooks
git add .claude/hooks/pre-compact.sh .claude/hooks/periodic-save.sh
git commit -m "chore(mempalace): task 05 - pre-compact + periodic-save hooks"
```

---

## Task 6: 注册 schtasks 计划任务

**Files:** 无 (Windows 系统级)

- [ ] **Step 1: 创建 schtasks 任务**

```bash
schtasks /create ^
    /tn "MempalacePeriodicSave" ^
    /tr "bash C:\Users\zhu\quant_trading\.claude\hooks\periodic-save.sh" ^
    /sc minute ^
    /mo 30 ^
    /rl highest ^
    /f
# 期望: SUCCESS: The scheduled task "MempalacePeriodicSave" has successfully been created.
```

- [ ] **Step 2: 验证任务存在**

```bash
schtasks /query /tn "MempalacePeriodicSave" /v /fo list
# 期望: 出现 "TaskName: MempalacePeriodicSave"
#       Next Run Time: <某个未来时间>
#       Status: Could be "Ready" or "Running" or "Never started"
```

- [ ] **Step 3: 立即跑一次测试(不真等到 30 分钟)**

```bash
schtasks /run /tn "MempalacePeriodicSave"
# 期望: SUCCESS: Attempted to run the scheduled task "MempalacePeriodicSave".
```

```bash
sleep 3
cat C:\Users\zhu\.mempalace\logs\periodic-save.log
# 期望: 看到 "[date] Periodic sweep start" 和 "sweep done"
```

- [ ] **Step 4: 卸载任务(若日后不需要,见 spec §9)**

```bash
# 卸载命令(本次不执行,只确认命令存在)
schtasks /delete /tn "MempalacePeriodicSave" /f
# 期望: SUCCESS: The scheduled task "MempalacePeriodicSave" was successfully deleted.
```

**然后重建** (Step 5):

- [ ] **Step 5: 重建任务(如果 Step 4 删了)**

如果 Step 4 真卸了,重跑 Step 1 重建。

- [ ] **Step 6: Commit milestone**

```bash
git checkout -b chore/mempalace-schtasks
echo "## Task 6: schtasks 计划任务已注册

- 任务名: MempalacePeriodicSave
- 周期: 每 30 分钟
- 命令: bash C:\\Users\\zhu\\quant_trading\\.claude\\hooks\\periodic-save.sh
- 卸载: schtasks /delete /tn \"MempalacePeriodicSave\" /f
" > docs/superpowers/install-logs/task-06-schtasks.md
git add docs/superpowers/install-logs/task-06-schtasks.md
git commit -m "chore(mempalace): task 06 - schtasks registered"
```

---

## Task 7: 配 MCP server + Claude Code 钩子注册

**Files:**
- Modify or Create: `C:\Users\zhu\quant_trading\.claude\settings.local.json`
- Modify: `C:\Users\zhu\quant_trading\.gitignore` (加 `.claude/hooks/*.log`)

- [ ] **Step 1: 检查 settings.local.json 是否存在**

```bash
ls -la C:\Users\zhu\quant_trading\.claude\settings.local.json 2>/dev/null || echo "NOT FOUND"
```

- [ ] **Step 2A: 不存在则新建**

文件 `C:\Users\zhu\quant_trading\.claude\settings.local.json`,完整内容:

```json
{
  "mcpServers": {
    "mempalace": {
      "command": "mempalace",
      "args": ["mcp", "serve", "--wing", "quant-trading"]
    }
  },
  "hooks": {
    "PreCompact": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "bash C:\\Users\\zhu\\quant_trading\\.claude\\hooks\\pre-compact.sh"
          }
        ]
      }
    ]
  }
}
```

- [ ] **Step 2B: 存在则只 merge 两段(用 Read + Edit)**

先 Read 看现有内容,定位:
- 如果已有 `mcpServers` 字段 → 在它下面加 `"mempalace": {...}` 块
- 如果没有 → 在顶层加 `"mcpServers": {...}` 块
- 同样处理 `hooks.PreCompact`

**不要覆盖用户已有设置**。把变更前内容 `git diff` 出来给用户看。

- [ ] **Step 3: 验 JSON 合法**

```bash
python -c "import json; json.load(open(r'C:\Users\zhu\quant_trading\.claude\settings.local.json', encoding='utf-8'))" && echo "JSON OK"
# 期望: JSON OK
```

- [ ] **Step 4: 改 .gitignore(加钩子日志忽略)**

先 Read 现有 `.gitignore`,找到合适的插入点(若文件不存在则新建)。

如果文件不存在,新建 `C:\Users\zhu\quant_trading\.gitignore`,内容:

```
# MemPalace hooks 日志
.claude/hooks/*.log
.claude/hooks/*.log.*
```

如果文件已存在,找到 `**/.claude/` 或类似模式后面追加:

```
# MemPalace hooks 日志
.claude/hooks/*.log
.claude/hooks/*.log.*
```

- [ ] **Step 5: 重启 Claude Code 验 MCP**

**关键说明**: 这一步**需要用户手动重启 Claude Code**,因为我无法重启自己。

让用户:
1. 完全退出当前 Claude Code
2. 重新打开 quant_trading 项目
3. 在新对话里说"列出 MCP 工具"
4. 期望看到 5 个: `mempalace_search` / `mempalace_mine` / `mempalace_wake_up` / `mempalace_list_agents` / `mempalace_get_drawer`

如果用户报告工具未出现,排查:
- `mempalace` PATH 问题 → `where mempalace`
- JSON 不合法 → 重新跑 Step 3
- settings.local.json 路径不对 → 检查 `.claude/` 目录在哪

- [ ] **Step 6: 全链路 smoke test**

如果 MCP 工具出现,跑一次端到端:
```
(mempalace_search 工具调用)
query: "XAUUSD M15 趋势策略"
wing: quant-trading
limit: 3
```

期望返回 ≥1 条 drawer,内容是 verbatim 原文。

- [ ] **Step 7: 写 README 段落(给项目未来的自己看)**

文件 `C:\Users\zhu\quant_trading\.claude\hooks\README.md`,内容:

```markdown
# MemPalace 自动保存钩子

## 钩子清单

| 钩子 | 触发 | 做什么 |
|------|------|--------|
| `pre-compact.sh` | Claude Code 准备压缩 context 前 | 强制 sweep 当前会话入库 |
| `periodic-save.sh` | schtasks 每 30 分钟 | 增量 sweep `~/.claude/projects/` 下所有 JSONL |

## 守卫逻辑

两个钩子首行都检查 `CLAUDE_PROJECT_DIR` 是否等于本项目路径。
切到其他项目时,钩子**立即退出 0**,不污染其他项目的宫殿。

## 守卫逻辑验证

```bash
# 在别的目录跑,应秒退
cd /tmp
CLAUDE_PROJECT_DIR="C:\\other" bash C:\Users\zhu\quant_trading\.claude\hooks\periodic-save.sh --dry-run
echo $?  # 期望 0,无输出
```

## 日志位置

- `~/.mempalace/logs/pre-compact.log`
- `~/.mempalace/logs/periodic-save.log`

## 卸载

```bash
# 1. 卸计划任务
schtasks /delete /tn "MempalacePeriodicSave" /f

# 2. 删钩子
rm -rf C:\Users\zhu\quant_trading\.claude\hooks

# 3. 改 .claude/settings.local.json,删 mcpServers.mempalace + hooks.PreCompact
```

完整卸载见 `docs/superpowers/specs/2026-06-07-mempalace-install-design.md` §9。
```

- [ ] **Step 8: Commit**

```bash
git checkout -b chore/mempalace-mcp-register
git add .claude/settings.local.json .gitignore .claude/hooks/README.md
git commit -m "chore(mempalace): task 07 - MCP registered, PreCompact hook wired, smoke tested"
```

---

## 验收 (Definition of Done)

7 步全过才算 install 完成。每步 fail → 立即停。

- [ ] **AC1**: `mempalace --version` 报 3.4.0+
- [ ] **AC2**: `~/.mempalace/projects/chroma/` 和 `sessions/chroma/` 各有 chroma.sqlite3
- [ ] **AC3**: `mempalace search "XAUUSD M15" --wing quant-trading` 至少 1 条 drawer
- [ ] **AC4**: `mempalace search "ECC 闪烁" --wing claude-sessions` 至少 1 条 drawer
- [ ] **AC5**: `schtasks /query /tn "MempalacePeriodicSave"` 显示任务存在
- [ ] **AC6**: Claude Code 重启后,5 个 mempalace_* MCP 工具出现
- [ ] **AC7**: 用 MCP 工具搜出 1 条 quant_trading 相关的 drawer
- [ ] **AC8**: 守卫验证通过(切到别项目,钩子秒退)

---

## 失败回滚 (任一 AC 失败时)

```bash
# 1. 卸 CLI
uv tool uninstall mempalace

# 2. 清数据
rm -rf ~/.mempalace

# 3. 卸计划任务
schtasks /delete /tn "MempalacePeriodicSave" /f

# 4. 清项目内钩子
rm -rf C:\Users\zhu\quant_trading\.claude\hooks
rm -f C:\Users\zhu\quant_trading\.claude\settings.local.json

# 5. 改 .gitignore(可选,删 .claude/hooks/*.log 那两行)

# 6. Git 还原
git checkout main
git branch -D chore/mempalace-install chore/mempalace-init chore/mempalace-mine-project \
              chore/mempalace-mine-sessions chore/mempalace-hooks chore/mempalace-schtasks \
              chore/mempalace-mcp-register
```

5 分钟内可完全还原到没装之前的状态。9 条文件记忆不受影响。

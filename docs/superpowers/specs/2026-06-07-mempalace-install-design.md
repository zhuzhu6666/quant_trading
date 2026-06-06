# MemPalace 全套安装设计 spec

- **日期**: 2026-06-07
- **作者**: Claude Code(经 brainstorming 五段确认)
- **目标项目**: `C:\Users\zhu\quant_trading` (用户: zhu)
- **目标系统**: Windows 11 Pro, Python 3.12, uv 0.11.19, 无 Docker

## 1. 背景与目标

### 1.1 当前现状
- 用户已有 9 条文件式记忆 (`~/.claude/projects/C--Users-zhu-quant-trading/memory/`),session 启动时全量塞进 context
- 容量天花板约几十条;超过则 context 爆
- 项目根 `C:\Users\zhu\quant_trading` 已有几百 commit 长期演化,后续需要"语义级"找回历史讨论/代码/决策

### 1.2 目标
安装 MemPalace v3.4.x (LongMemEval R@5 96.6% raw),让 quant_trading 项目拥有:
- 语义级代码/文档/脚本检索
- Claude Code 会话自动入库(防 30 天过期)
- MCP server 让 Claude Code 直接调用 5 个核心工具
- 钩子守卫:只在 quant_trading 项目下激活,不污染其他项目

### 1.3 非目标 (YAGNI)
- LLM rerank (需要 API key,默认 96.6% 已够)
- 外部后端 (Qdrant / pgvector) — 本地 ChromaDB 够用
- Web UI
- 知识图谱时序功能
- sweep 后台守护进程 (用钩子代替)
- 多用户 / 多机同步

## 2. 整体架构

### 2.1 目录布局
```
C:\Users\zhu\                                  ← 用户的家目录
├── .mempalace/                                ← 宫殿根(全在这,项目根零痕迹)
│   ├── projects/                              ← 项目代码宫殿 (wing: quant-trading)
│   │   ├── chroma/                            ← ChromaDB 数据
│   │   ├── embed_cache/                       ← 嵌入模型缓存
│   │   ├── config.json
│   │   └── backends.json
│   ├── sessions/                              ← 会话独立宫殿 (wing: claude-sessions)
│   │   ├── chroma/
│   │   ├── embed_cache/
│   │   └── config.json
│   └── logs/                                  ← 钩子运行日志
│
└── .claude/                                   ← 现有 Claude Code 配置
    ├── projects/.../memory/                   ← 9 条文件记忆,不动
    └── settings.json                          ← 全局配置,不动

C:\Users\zhu\quant_trading\                    ← 项目根,零 .mempalace
└── .claude/                                   ← 已有 .claude-setup,扩展为:
    ├── settings.local.json  ← 新增 MCP server 注册
    └── hooks/                                 ← 新增,条件钩子
        ├── pre-compact.sh
        └── periodic-save.sh
```

### 2.2 数据流
```
quant_trading 项目文件 ──mine--mode files──▶ ~/.mempalace/projects/chroma/ (wing: quant-trading)
                │
                └─MCP server (mempalace mcp serve --wing quant-trading)──▶ Claude Code 工具
                                                                              │
~/.claude/projects/  ◀─hook PreCompact + 周期 sweep──────────────────────────────┘
   (历史 JSONL)
        │
        └─▶ ~/.mempalace/sessions/chroma/ (wing: claude-sessions) — 不挂 MCP,只 CLI
```

### 2.3 关键设计点
- **所有数据在 `~/.mempalace/`**,项目根零宫殿痕迹
- **钩子放项目根 `.claude/hooks/`**,跟随项目走,切到别的项目时守卫脚本秒退
- **会话 wing 独立**:`claude-sessions` 不挂 MCP,只 CLI 检索,避免检索自己会话灌爆 context
- **MCP 只暴露 `quant-trading` wing**:5 个核心工具 (search / mine / wake_up / list_agents / get_drawer)

## 3. Wing 与 Room 划分

### 3.1 Wing 一览

| Wing 名 | 后端 | 吸什么 | 存哪 |
|---------|------|--------|------|
| `quant-trading` | ChromaDB | 项目代码 / MD / 配置 | `~/.mempalace/projects/chroma/` |
| `claude-sessions` | ChromaDB | `~/.claude/projects/*.jsonl` | `~/.mempalace/sessions/chroma/` |

### 3.2 `quant-trading` wing 内部 Room (按子目录自动推)

| Room | 来源 | 备注 |
|------|------|------|
| `code` | `alpha/`, `strategy/`, `execution/`, `risk/`, `data/`, `config/` | Python 业务代码 |
| `docs` | `*.md`, `docs/`, `ROADMAP.md`, `PROJECT_AUDIT*.md` | 文档与决策记录 |
| `scripts` | `scripts/` | 一次性脚本 |
| `root` | `main.py`, `requirements.txt`, `README.md`, `pyproject.toml` | 根级入口 |

### 3.3 `claude-sessions` wing 内部 Room

| Room | 来源 |
|------|------|
| `current-project` | `~/.claude/projects/C--Users-zhu-quant-trading/**` 下的所有 `.jsonl` |
| `other` | 别的项目会话(若存在) |

### 3.4 Mine 命令序列
```bash
# 项目代码 (wing: quant-trading, 按子目录推 room)
mempalace mine C:\Users\zhu\quant_trading --wing quant-trading --mode files

# 历史会话 (wing: claude-sessions, 按子目录推 room)
mempalace mine C:\Users\zhu\.claude\projects --wing claude-sessions --mode convos
```

**不启用 `sweep` 守护进程**——`sweep` 与钩子职责重叠,会双写并去重冲突。钩子已覆盖增量入库需求。

## 4. Hooks 条件钩子

### 4.1 钩子 A: `periodic-save.sh`(定期保存)
- **触发方式**: **不依赖 Claude Code 自带的定时语义**(Claude Code 钩子系统无 cron)。实施时另起一个 Windows 计划任务 (`schtasks /create /sc minute /mo 30`),每 30 分钟调用一次该脚本
- **位置**: `C:\Users\zhu\quant_trading\.claude\hooks\periodic-save.sh`
- **做什么**:
  1. 守卫: `CLAUDE_PROJECT_DIR != "C:\Users\zhu\quant_trading"` 时立即 `exit 0`
  2. 扫 `~/.claude/projects/C--Users-zhu-quant-trading/` 下的 `.jsonl`
  3. `mempalace sweep <dir> --wing claude-sessions` 增量入库(消息 hash 幂等去重)
  4. 失败:写 `~/.mempalace/logs/periodic-save.log`,绝不阻塞 Claude
- **退出码**: 永远 0

### 4.2 钩子 B: `pre-compact.sh`(压缩前保存)
- **触发时机**: `PreCompact` 事件(Claude Code 准备压缩 context 前)
- **位置**: `C:\Users\zhu\quant_trading\.claude\hooks\pre-compact.sh`
- **做什么**:
  1. 守卫: 同 4.1
  2. 从 stdin 读 `{transcript_path, session_id}` (Claude Code 注入)
  3. `mempalace sweep <transcript_dir> --wing claude-sessions --force`
  4. 退出码 0
- **关键性**: README 强调的"30 天过期"防线

### 4.3 钩子配置 (`settings.local.json` 片段)
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

### 4.4 条件激活原理
- Claude Code 钩子随 `settings.local.json` 所在项目加载
- 钩子脚本**首行守卫**判断 `CLAUDE_PROJECT_DIR`,切到别的项目时秒退
- 用户多个项目并行互不污染

### 4.5 钩子验收命令
```bash
bash periodic-save.sh --dry-run
echo '{"transcript_path":"/tmp/fake.jsonl"}' | bash pre-compact.sh
```

## 5. MCP server 接入

### 5.1 注册方式
- 走 `uv tool install` 后落在 `%USERPROFILE%\.local\bin\mempalace.exe`
- Claude Code 直接 spawn,无需 Docker
- 只暴露 `quant-trading` wing(会话 wing 不挂 MCP)

### 5.2 暴露的 5 个核心工具
| 工具 | 用途 |
|------|------|
| `mempalace_search` | 语义检索代码/文档 |
| `mempalace_mine` | 增量入库新文件 |
| `mempalace_wake_up` | 新会话开头拉回历史脉络 |
| `mempalace_list_agents` | 发现宫殿里有哪些 wing |
| `mempalace_get_drawer` | 读某条 drawer 的 verbatim 原文 |

### 5.3 验收:Claude Code 重启后
- 对话里说 "列出 mcp 工具"
- 期望出现上述 5 个

## 6. 错误处理

| 失败点 | 退路 |
|--------|------|
| `uv tool install mempalace` 失败 | 立刻停,报原始 stderr,不动其他东西 |
| `mempalace init` 失败 | 删掉已建目录,回滚零状态 |
| `mempalace mine` 失败(部分文件) | 继续 mine 剩下的,只 log 失败文件 |
| Embedding 模型下载失败(网络) | 给两条路:`--model all-MiniLM-L6-v2` 重试 或 改走网络 |
| `mempalace sweep`(hook 内)失败 | 绝不阻塞 Claude,只写 log |
| MCP server 启动失败 | 钩子和数据都还在,Claude 工具列表少 5 个,手用 CLI 不受影响 |
| ChromaDB 损坏 | `rm -rf ~/.mempalace` 重建,文件源不动 |
| 钩子配置写错导致 Claude Code 启动报错 | `.claude/settings.local.json` 备份后再改,出问题时 `git checkout` 恢复 |

## 7. 验收测试 (Definition of Done)

6 步全过才算"安装完成":

```bash
# 1. CLI 装好没
mempalace --version
# 期望: mempalace, version 3.4.0+

# 2. 宫殿初始化成功
ls ~/.mempalace/projects/chroma/    # 期望有 chroma.sqlite3 等
ls ~/.mempalace/sessions/chroma/

# 3. mine 干跑
mempalace mine C:\Users\zhu\quant_trading --wing quant-trading --mode files --dry-run
# 期望: 报告预计入库文件数 >= 50

# 4. 真跑 mine + search
mempalace mine C:\Users\zhu\quant_trading --wing quant-trading --mode files
mempalace search "XAUUSD M15 趋势" --wing quant-trading
# 期望: 至少返回 1 条 drawer

# 5. 钩子干跑
bash C:\Users\zhu\quant_trading\.claude\hooks\pre-compact.sh --dry-run
bash C:\Users\zhu\quant_trading\.claude\hooks\periodic-save.sh --dry-run
# 期望: 退出码 0, log 输出 "would sweep N files"

# 6. MCP 注册生效 (需 Claude Code 重启)
# 对话里说 "列出 mcp 工具" → 期望出现 5 个 mempalace_*
```

任一步失败 → 立即停,贴错误,问下一步。

## 8. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 首次 mine 耗时 5-15 分钟 (~1000+ 文件 embed) | 装前确认;后台跑可问用户 |
| Embedding 模型 300MB 一次性下载 | 装前确认磁盘剩余 >= 1GB |
| 钩子与现有 9 条文件记忆冲突 | 文件记忆 session 启动加载;新 hook 是逐字+语义层;两层并行不冲突 |
| Windows 路径反斜杠转义 | 装前 `where mempalace` 验可执行,JSON 里用 `\\` |
| `--mode` 选错破坏语义 | 第一次就分对:`files` 给代码,`convos` 给会话 |
| MCP 工具调用 3 次都不准 | 上 LLM rerank 模式(需 API key,**到时再决策**) |
| 仿冒 PyPI 包 | 只用官方 `pip install mempalace` / `uv tool install mempalace`,不引第三方源 |

## 9. 卸载路径

后悔成本:
```bash
# 1. 卸载 CLI
uv tool uninstall mempalace

# 2. 清掉所有数据
rm -rf ~/.mempalace

# 3. 清掉项目内钩子
rm -rf C:\Users\zhu\quant_trading\.claude\hooks

# 4. 清掉计划任务
schtasks /delete /tn "MempalacePeriodicSave" /f

# 5. 改 .claude/settings.local.json
#    - 删 mcpServers.mempalace 段
#    - 删 hooks.PreCompact 段
#    实施时若该文件不存在则新建;若存在则只删这两段,绝不覆盖用户已有设置
```

5 步内可完全还原。9 条文件记忆不受影响。

## 10. 实施前的前置确认

实施 plan 启动前,我需要向用户最后确认 1 个时间窗口问题:
- 首次 mine 预计 5-15 分钟,是否接受前台跑?不接受就后台跑(用 `run_in_background: true`)

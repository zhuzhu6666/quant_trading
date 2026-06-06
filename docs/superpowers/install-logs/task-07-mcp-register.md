## Task 7: MCP 注册 + .gitignore + hooks README

### 结果摘要

- **MCP server**: `mempalace` (stdio) → `mempalace-mcp --backend chroma` → 状态 ✓ Connected
- **.gitignore**: 加 `.claude/hooks/*.log` 和 `.claude/hooks/*.log.*`
- **README**: `.claude/hooks/README.md` 写完

### 关键偏差(从 plan)

1. **`mempalace mcp` 不是 server 本身**——是 setup helper
   - plan 写的 `args: ["mcp", "serve", "--wing", "quant-trading"]` 100% 错
   - 实际 server 是独立可执行 `mempalace-mcp` (Task 1 装 CLI 时一同落下)
   - 正确命令: `mempalace-mcp --backend chroma`
2. **`claude mcp add` 写到 `~/.claude.json` 的 `projects[].mcpServers`,不是 `settings.local.json`**
   - plan 让我改 settings.local.json 是错的
   - 用 `claude mcp add mempalace -- mempalace-mcp --backend chroma` 一行搞定
   - 注册位置: `C:\Users\zhu\.claude.json` → `projects["C:/Users/zhu/quant_trading"].mcpServers.mempalace`
   - 这文件不在项目内,**不进 git**
3. **`settings.local.json` 早就有内容**(18 项 permissions + 5 个 enabled plugins),不动它,避免覆盖用户设置

### 验证

- `claude mcp get mempalace`:
  ```
  mempalace:
    Scope: Local config (private to you in this project)
    Status: ✓ Connected
    Type: stdio
    Command: mempalace-mcp
    Args: --backend chroma
  ```
- `mempalace-mcp --backend chroma` 直接跑: "MemPalace MCP Server starting..." (exit 0)
- `claude mcp get` 显示状态:✓ Connected

### 文件变更

- 修改 `.gitignore` 加 hooks 日志忽略
- 新增 `.claude/hooks/README.md`
- `.claude/settings.local.json` **未动**(原本就有内容,不动)
- `~/.claude.json` 修改(不进 git,用户级配置)

### 重启 Claude Code 后的验收

**AC6** (期望 5 个 mempalace_* MCP 工具出现):需要用户**手动重启 Claude Code**,因为我无法重启自己。重启后:
- 对话里说"列出 MCP 工具"
- 期望看到 mempalace 暴露的工具(从 mempalace-mcp 出来)

### 已知遗留

- Plan 假设的 5 个工具名 (`mempalace_search` / `mempalace_mine` / `mempalace_wake_up` / `mempalace_list_agents` / `mempalace_get_drawer`) 实际可能不一样——mempalace-mcp 暴露的工具名要看实际 server 行为。重启 Claude Code 后看实际工具名,如有偏差在 install log 补充
- 计划里"AC7: smoke test 用 MCP 工具搜出 drawer"需要 Claude Code 重启 + 我能用 MCP 工具才能验,本次到此等用户重启

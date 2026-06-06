## Task 5: 写两个钩子脚本

### 结果摘要

- **pre-compact.sh** (54 lines, post-fix): PreCompact 事件触发,sweep 当前会话到 claude-sessions wing
- **periodic-save.sh** (51 lines, post-fix): schtasks 触发,同样的 sweep,带 `--dry-run` 验收模式
- **run-periodic-save.bat** (4 lines): schtasks 包装器,cd + 设 env + 调 bash

### 关键偏差(从 plan)

1. **`mempalace sweep` 只接受位置参数,无 `--wing`/`--force`**:
   - 计划写的 `mempalace sweep $dir --wing claude-sessions --force` 错
   - 实际: `mempalace sweep <target>`,wing 由 CWD 决定
   - 修法: sweep 前 `cd "$PALACE_DIR"` 进 palace 目录
   - `--force` 不需要, sweep 本身幂等(message-hash 去重 + cursor 跳过)
2. **`date -Iseconds` 在 schtasks 启的 bash 里找不到** (Task 6 暴露):
   - 改用 bash 内建 `printf '%(%Y-%m-%dT%H:%M:%S%z)T' -1`,纯内建不依赖外部命令
   - pre-compact.sh 和 periodic-save.sh 都改
3. **守卫需要 `CLAUDE_PROJECT_DIR` env**: schtasks 不会注入, .bat 包装器补设 (Task 6 修)
4. **Windows 路径格式**: `EXPECTED_PROJECT` 用**单反斜杠** `C:\Users\zhu\quant_trading` 才匹配 Claude Code 注入

### 验证

- 干跑: `echo '{"transcript_path":"C:/tmp/fake.jsonl","session_id":"test"}' | CLAUDE_PROJECT_DIR="C:\Users\zhu\quant_trading" bash .claude/hooks/pre-compact.sh` → exit 0, log 写入 sweep done
- 干跑 periodic-save: `bash .claude/hooks/periodic-save.sh --dry-run` → exit 0, "would sweep N jsonl files"
- 守卫 (切到 /tmp, CLAUDE_PROJECT_DIR="C:\other"): 两个钩子都 exit 0 静默秒退
- 守卫 (无 CLAUDE_PROJECT_DIR): pre-compact.sh 静默, periodic-save.sh 静默 (`$(pwd)` fallback 是 `C:\WINDOWS\system32` 不匹配)

### Commit

`0533a65 chore(mempalace): task 05 - pre-compact + periodic-save hooks` (合并到 main: `363321d`)

### 真实 log 输出 (sweep 在干跑里跑了)

```
[2026-06-07T02:34:18+08:00] PreCompact sweep start
  transcript_path: C:/tmp/fake.jsonl
  sessions_root: C:\Users\zhu\.claude\projects\C--Users-zhu-quant-trading
  palace_dir: C:\Users\zhu\.mempalace\sessions
  Swept 59/59 files from C:\Users\zhu\.claude\projects\C--Users-zhu-quant-trading: +56 new, 47 already present, 9690 skipped (< cursor).
[2026-06-07T02:34:35+08:00] PreCompact sweep done
```

#!/usr/bin/env bash
# Periodic save hook: 由 schtasks 每 30 分钟触发
# 守卫: 只在 quant_trading 项目下激活

set -u

# === 守卫: 跨项目秒退 ===
# Note: 字符串必须用单反斜杠(Claude Code 在 Windows 上注入的格式就是 C:\Users\...)
EXPECTED_PROJECT="C:\Users\zhu\quant_trading"
ACTUAL_PROJECT="${CLAUDE_PROJECT_DIR:-$(pwd)}"
if [ "$ACTUAL_PROJECT" != "$EXPECTED_PROJECT" ]; then
    # 守卫触发,绝不算错
    exit 0
fi

# === dry-run 模式 (验收用) ===
if [ "${1:-}" = "--dry-run" ]; then
    SESSIONS_ROOT="C:\Users\zhu\.claude\projects\C--Users-zhu-quant-trading"
    if [ -d "$SESSIONS_ROOT" ]; then
        N=$(find "$SESSIONS_ROOT" -name "*.jsonl" 2>/dev/null | wc -l)
        echo "[dry-run] would sweep $N jsonl files in $SESSIONS_ROOT"
    else
        echo "[dry-run] sessions dir not found: $SESSIONS_ROOT"
    fi
    exit 0
fi

# === 跑 sweep,失败不阻塞 ===
SESSIONS_ROOT="C:\Users\zhu\.claude\projects\C--Users-zhu-quant-trading"
PALACE_DIR="C:\Users\zhu\.mempalace\sessions"
LOG_FILE="C:\Users\zhu\.mempalace\logs\periodic-save.log"
mkdir -p "$(dirname "$LOG_FILE")"

if [ ! -d "$SESSIONS_ROOT" ] || [ ! -d "$PALACE_DIR" ]; then
    # 没会话目录,静默
    exit 0
fi

# 关键 deviation: mempalace sweep 没有 --wing/--force flag
# wing 由 CWD 决定,所以必须 cd 到 palace dir
# sweep 是幂等的(message-level + cursor),所以不用 --force
# 用 bash 内建 printf '%()T' 取时间,避免依赖 date 命令 (schtasks 启的 bash PATH 缺 date)
NOW() { printf '%(%Y-%m-%dT%H:%M:%S%z)T' -1; }
{
    echo "[$(NOW)] Periodic sweep start"
    (cd "$PALACE_DIR" && mempalace sweep "$SESSIONS_ROOT" 2>&1) || echo "  sweep exited non-zero, ignoring"
    echo "[$(NOW)] Periodic sweep done"
} >> "$LOG_FILE" 2>&1

exit 0

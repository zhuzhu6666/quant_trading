#!/usr/bin/env bash
# PreCompact hook: 压缩 context 前自动 sweep 当前会话入库
# 守卫: 只在 quant_trading 项目下激活

set -u

# === 守卫: 跨项目秒退 ===
# Note: 字符串必须用单反斜杠(Claude Code 在 Windows 上注入的格式就是 C:\Users\...)
EXPECTED_PROJECT="C:\Users\zhu\quant_trading"
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
SESSIONS_ROOT="C:\Users\zhu\.claude\projects\C--Users-zhu-quant-trading"
PALACE_DIR="C:\Users\zhu\.mempalace\sessions"
if [ ! -d "$SESSIONS_ROOT" ] || [ ! -d "$PALACE_DIR" ]; then
    # 目录不存在,静默失败
    exit 0
fi

# === 跑 sweep,失败不阻塞 ===
# 关键 deviation: mempalace sweep 没有 --wing/--force flag
# wing 由 CWD 决定,所以必须 cd 到 palace dir
# sweep 是幂等的(message-level + cursor),所以不用 --force
LOG_FILE="C:\Users\zhu\.mempalace\logs\pre-compact.log"
# PATH-independent: bash-only dirname + best-effort mkdir
LOG_DIR="${LOG_FILE%/*}"; [ "$LOG_DIR" = "$LOG_FILE" ] && LOG_DIR="${LOG_FILE%\\*}"
[ "$LOG_DIR" = "$LOG_FILE" ] && LOG_DIR="."
[ -d "$LOG_DIR" ] || mkdir -p "$LOG_DIR" 2>/dev/null || true

{
    NOW() { printf '%(%Y-%m-%dT%H:%M:%S%z)T' -1; }
    echo "[$(NOW)] PreCompact sweep start"
    echo "  transcript_path: $TRANSCRIPT_PATH"
    echo "  sessions_root: $SESSIONS_ROOT"
    echo "  palace_dir: $PALACE_DIR"

    (cd "$PALACE_DIR" && mempalace sweep "$SESSIONS_ROOT" 2>&1) || echo "  sweep exited non-zero, ignoring"

    echo "[$(NOW)] PreCompact sweep done"
} >> "$LOG_FILE" 2>&1

# 永远 0
exit 0

"""硬规则 R1 的守卫：系统只允许在「零状态」下启动。

设计原则（原则 6 —— 代码质量优先于归因量）
────────────────────────────────────────────
这个脚本**什么都不做**。

  不恢复、不平仓、不补归因、不对账、不重放。

它只把「存在状态」变成一个**硬失败**，逼人在系统外处理。
理由：带持仓重启需要的恢复代码是一条三段式污染链
（见 docs/量化系统_模块缺陷与整合修复方案.md §L0-0），
且其产物必然是**猜测**而非**事实**（实测 restart_replay 占平仓归因 21.3%）。

用一个"零状态"前提消灭这个场景，比维护它便宜得多。

零状态定义
──────────
  1. 无持仓        —— recovery_position_state 中 status='open'
  2. 无未决意图    —— broker_execution_intent 中未终结的意图

用法
────
    .venv/bin/python scripts/preflight_state_guard.py

退出码
──────
    0 —— 零状态，允许启动
    1 —— 存在状态，拒绝启动（systemd 会因此中止本次启动）
    2 —— 无法判定（连接失败等），同样拒绝启动（fail-closed）
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _fail_closed(reason: str) -> None:
    """无法判定时拒绝启动。

    fail-closed 是刻意的：宁可因数据库不可用而拒绝启动，
    也不要因为"查不到就当作没有"而放行一次带状态启动 ——
    后者的代价是不可逆的归因污染。
    """
    print(f"REFUSING TO START: cannot determine state ({reason})", file=sys.stderr)
    print("  This guard fails closed on purpose: an unverifiable start is", file=sys.stderr)
    print("  treated as an unsafe start.", file=sys.stderr)
    sys.exit(2)


def _open_positions(conn) -> list[tuple[str, str, float]]:
    """返回 [(position_id, symbol, volume)]，status='open' 的仓位。

    注意：``get_state_pg_conn()`` 返回的游标产出 **dict**（row_factory 已设），
    不是 tuple。对这个环境用 r[0] 会静默失败或抛 KeyError。
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT position_id, coalesce(symbol, '') AS symbol, coalesce(volume, 0.0) AS volume
        FROM runtime.recovery_position_state
        WHERE status = 'open'
        ORDER BY position_id
        """
    )
    return [
        (str(r["position_id"]), str(r["symbol"] or ""), float(r["volume"] or 0.0))
        for r in cur.fetchall()
    ]


def _pending_intents(conn) -> list[tuple[str, float]]:
    """返回 [(intent_id, created_at)]，尚未终结的执行意图。

    终结状态集合刻意写死：未知状态视为「未决」，
    因为对一个守卫来说，误报（多拦一次）远比漏报便宜。
    """
    terminal = ("confirmed", "rejected", "failed", "cancelled", "expired")
    cur = conn.cursor()
    cur.execute(
        """
        SELECT intent_id, coalesce(created_at, 0.0) AS created_at, coalesce(status, '') AS status
        FROM runtime.broker_execution_intent
        WHERE coalesce(status, '') <> ALL(%s)
        ORDER BY created_at DESC
        """,
        (list(terminal),),
    )
    return [(str(r["intent_id"]), float(r["created_at"] or 0.0)) for r in cur.fetchall()]


def main() -> int:
    try:
        from backend.core.db import get_state_pg_conn
    except Exception as exc:  # noqa: BLE001
        _fail_closed(f"import backend.core.db failed: {exc}")

    try:
        conn = get_state_pg_conn(read_only=True)
    except Exception as exc:  # noqa: BLE001
        _fail_closed(f"cannot connect to state store: {exc}")

    try:
        positions = _open_positions(conn)
        pending = _pending_intents(conn)
    except Exception as exc:  # noqa: BLE001
        _fail_closed(f"state query failed: {exc}")
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    if not positions and not pending:
        print("OK: zero-state confirmed (no open positions, no pending intents)")
        return 0

    print("REFUSING TO START: system is not in a zero-state", file=sys.stderr)
    if positions:
        print(f"  open positions ({len(positions)}):", file=sys.stderr)
        for pid, sym, vol in positions:
            print(f"    - {pid}  {sym}  volume={vol}", file=sys.stderr)
    if pending:
        print(f"  pending intents ({len(pending)}):", file=sys.stderr)
        for iid, ts in pending:
            print(f"    - {iid}  created_at={ts}", file=sys.stderr)
    print(file=sys.stderr)
    print("  Flatten in the broker terminal, then start. Attribution for these", file=sys.stderr)
    print("  positions is intentionally discarded (hard rule R1) -- the recovery", file=sys.stderr)
    print("  path that would have guessed it has been removed on purpose.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""
scripts/refresh_external_data.py — 外部数据自动刷新（替代手动脚本）

自动检测各外部数据源的时效性, 按频率自动拉取更新。
可被后端启动流程、计划任务或手动命令调用。

用法:
    python scripts/refresh_external_data.py              # 检查所有过期数据并更新
    python scripts/refresh_external_data.py --source cot  # 只刷新 COT
    python scripts/refresh_external_data.py --force       # 忽略时效检查, 强制全量刷新
    python scripts/refresh_external_data.py --status      # 只看各数据源时效, 不拉取
    python scripts/refresh_external_data.py --once        # 一次拉完退出 (cron 模式)

数据源频率:
  - cot_gold:     周度 (CFTC 周二发布, 周五 COT 报告)
  - events:       日度 (ForexFactory 日历)
  - etf_holdings: 季度 (GLD/SLV SEC 10-Q, 硬拉)
  - mt5_bars:     ⏸ 阻塞 (MetaTrader5 包不兼容)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Windows GBK 编码兼容: ✓ ✗ 等 UTF-8 字符不崩终端
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.core.db import connect_duckdb
from data.store import DataStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("refresh_external")

DB_PATH = "data/ctrader_data.duckdb"

# ── 数据源频率定义 (秒) ──────────────────────────────────
FREQ = {
    "cot":        7 * 86400,      # 周度
    "events":     86400,          # 日度
    "etf":        90 * 86400,     # 季度
}


def _get_store() -> DataStore:
    return DataStore(DB_PATH)


# ── Status: 查各表最新时间 ──────────────────────────────

def _get_latest_timestamp(store: DataStore, table: str, date_col: str) -> datetime | None:
    """查表中最新的时间戳"""
    con = connect_duckdb(DB_PATH, read_only=True)
    try:
        row = con.execute(f"SELECT MAX({date_col}) FROM {table}").fetchone()
        if row and row[0]:
            ts = datetime.fromisoformat(str(row[0]))
            return ts
    except Exception:
        return None
    finally:
        con.close()
    return None


def _status_cot(store: DataStore) -> dict:
    """COT 最新数据日期"""
    latest = _get_latest_timestamp(store, "cot_gold", "report_date")
    stale = True
    if latest:
        age = (datetime.now() - latest).total_seconds()
        stale = age > FREQ["cot"]
    return {
        "table": "cot_gold",
        "latest": latest.strftime("%Y-%m-%d") if latest else "空表",
        "stale": stale,
        "age_days": round((datetime.now() - latest).total_seconds() / 86400, 1) if latest else None,
    }


def _status_events(store: DataStore) -> dict:
    """events 表: 查最晚事件日期"""
    latest = _get_latest_timestamp(store, "events", "date")
    stale = True
    if latest:
        age = (datetime.now() - latest).total_seconds()
        stale = age > FREQ["events"]
    return {
        "table": "events",
        "latest": latest.strftime("%Y-%m-%d") if latest else "空表",
        "stale": stale,
        "age_days": round((datetime.now() - latest).total_seconds() / 86400, 1) if latest else None,
    }


def _status_etf(store: DataStore) -> dict:
    """etf_holdings 最新日期"""
    latest = _get_latest_timestamp(store, "etf_holdings", "date")
    stale = True
    if latest:
        age = (datetime.now() - latest).total_seconds()
        stale = age > FREQ["etf"]
    return {
        "table": "etf_holdings",
        "latest": latest.strftime("%Y-%m-%d") if latest else "空表",
        "stale": stale,
        "age_days": round((datetime.now() - latest).total_seconds() / 86400, 1) if latest else None,
    }


def status_all(store: DataStore | None = None) -> list[dict]:
    return [
        _status_cot(store),
        _status_events(store),
        _status_etf(store),
    ]


# ── Refreshers ──────────────────────────────────────────

def refresh_cot(force: bool = False) -> bool:
    """拉取 CFTC COT 黄金持仓 (周度)"""
    store = _get_store()
    s = _status_cot(store)
    if not force and not s["stale"]:
        log.info(f"COT 未过期 (最新 {s['latest']}), skip")
        return True

    log.info("[COT] 从 CFTC 下载黄金持仓数据 ...")
    try:
        # 复用 load_cot_gold 的核心逻辑
        from scripts.load_cot_gold import download_year, parse_year
    except ImportError:
        log.error("无法 import scripts.load_cot_gold")
        return False

    current_year = datetime.now().year
    # 只刷最近 3 年 (2024-2026) — 旧数据已在库中
    years = list(range(current_year - 3, current_year + 1))
    all_dfs = []
    for y in years:
        try:
            zp = download_year(y)
            df = parse_year(zp)
            if not df.empty:
                log.info(f"  {y}: {len(df)} rows")
                all_dfs.append(df)
        except Exception as e:
            log.warning(f"  {y}: {e}")
            continue

    if not all_dfs:
        log.error("COT: 无数据加载")
        return False

    import pandas as pd
    df_all = pd.concat(all_dfs, ignore_index=True)
    df_all = df_all.drop_duplicates(subset=["report_date"]).sort_values("report_date").reset_index(drop=True)

    store = _get_store()
    n = 0
    for _, row in df_all.iterrows():
        store.insert_cot_gold(
            report_date=row["report_date"].strftime("%Y-%m-%d"),
            open_interest=int(row["open_interest"]),
            mm_long=int(row["mm_long"]),
            mm_short=int(row["mm_short"]),
            mm_spread=int(row["mm_spread"]),
            pm_long=int(row["pm_long"]),
            pm_short=int(row["pm_short"]),
            swap_long=int(row["swap_long"]),
            swap_short=int(row["swap_short"]),
            other_long=int(row["other_long"]),
            other_short=int(row["other_short"]),
        )
        n += 1
    if n == 0:
        log.warning("COT: 无新行写入 (可能已有最新数据)")
        return True
    log.info(f"COT: 写入 {n} 行 → cot_gold")
    return True


def refresh_events(force: bool = False) -> bool:
    """拉取 ForexFactory 经济日历 (日度)"""
    store = _get_store()
    s = _status_events(store)
    if not force and not s["stale"]:
        log.info(f"Events 未过期 (最新 {s['latest']}), skip")
        return True

    try:
        from scripts.fetch_events_calendar import fetch_events, parse_and_filter, upsert_events
    except ImportError:
        log.error("无法 import fetch_events_calendar")
        return False

    log.info("[Events] 从 ForexFactory 拉取经济日历 ...")
    try:
        events = fetch_events(weeks=2)
        if not events:
            log.warning("Events: 返回空")
            return False
        parsed = parse_and_filter(events)
        log.info(f"Events: 有效事件 {len(parsed)}/{len(events)} 条")
        if not parsed:
            log.warning("Events: 无有效事件 (非 USD 或被过滤)")
            return False
        result = upsert_events(parsed)
        inserted = result.get("inserted", 0) or result.get("upserted", 0)
        log.info(f"Events: 入库 {inserted} 条")
        return True
    except Exception as e:
        log.error(f"Events 刷新失败: {e}")
        return False


def refresh_etf(force: bool = False) -> bool:
    """从 SEC EDGAR 拉 GLD 持仓 (季度)"""
    store = _get_store()
    s = _status_etf(store)
    if not force and not s["stale"]:
        log.info(f"ETF 未过期 (最新 {s['latest']}), skip")
        return True

    try:
        from scripts.load_gld_holdings_sec import main as gld_main
    except ImportError:
        log.error("无法 import load_gld_holdings_sec")
        return False

    log.info("[ETF] 从 SEC EDGAR 拉 GLD 持仓 ...")
    try:
        gld_main()
        log.info("ETF: GLD holdings 刷新完成")
        return True
    except Exception as e:
        log.error(f"ETF 刷新失败: {e}")
        return False


# ── 主入口 ──────────────────────────────────────────────

SOURCES = {
    "cot": refresh_cot,
    "events": refresh_events,
    "etf": refresh_etf,
}


def print_status(status_list: list[dict]):
    """打印数据源时效表格"""
    print(f"{'数据源':<20s} {'表':<20s} {'最新日期':<20s} {'过期':<8s} {'说明'}")
    print("-" * 80)
    for s in status_list:
        stale = "⚠ 过期" if s.get("stale") else "✓ 正常"
        note = s.get("note", f"{s.get('age_days', '?')} 天前" if s.get("age_days") else "")
        print(f"{s.get('table', ''):<20s} {'':<20s} {str(s.get('latest', '?')):<20s} {stale:<8s} {note}")


def main():
    p = argparse.ArgumentParser(description="外部数据自动刷新")
    p.add_argument("--source", choices=list(SOURCES.keys()) + ["all"], default="all",
                   help="指定数据源 (默认 all)")
    p.add_argument("--force", action="store_true", help="强制刷新, 忽略时效检查")
    p.add_argument("--status", action="store_true", help="仅查看时效, 不刷新")
    p.add_argument("--once", action="store_true", help="cron 模式: 一次拉完退出")
    args = p.parse_args()

    if args.status:
        print_status(status_all())
        return

    if args.once:
        log.info("=== 外部数据自动刷新 (一次性) ===")

    sources = list(SOURCES.keys()) if args.source == "all" else [args.source]
    results = {}
    for src in sources:
        log.info(f"── {src} ──")
        try:
            ok = SOURCES[src](force=args.force)
            results[src] = "✓" if ok else "✗"
        except Exception as e:
            log.error(f"{src} 异常: {e}")
            results[src] = "✗"

    print()
    print("=" * 40)
    print(f"  外部数据刷新结果")
    print("=" * 40)
    for src, res in results.items():
        print(f"  {src:<10s} {res}")
    print()

    if args.once:
        print_status(status_all())

    all_ok = all(v == "✓" for v in results.values())
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())


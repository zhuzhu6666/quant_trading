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
from urllib.parse import urlencode

import requests

# Windows GBK 编码兼容: ✓ ✗ 等 UTF-8 字符不崩终端
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.core.db import DUCKDB_EVENTS, DUCKDB_EXTERNAL, connect_duckdb, duckdb_readonly_connection
from data.external_schema import (
    ensure_external_schema,
    cot_release_at,
    finish_refresh_audit,
    latest_audit_by_source,
    macro_release_at,
    record_raw_file,
    start_refresh_audit,
)
from data.store import DataStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("refresh_external")

DB_PATH = str(DUCKDB_EXTERNAL)
RAW_DIR = Path("data/external_raw")
RAW_DIR.mkdir(parents=True, exist_ok=True)
FRED_SERIES = ["DFII10", "DTWEXBGS", "DGS10", "T10YIE", "VIXCLS", "GVZCLS"]

# ── 数据源频率定义 (秒) ──────────────────────────────────
FREQ = {
    "cot":       10 * 86400,      # 周度报告有发布延迟
    "events":     86400,          # 日度
    "etf":       135 * 86400,     # 季度披露通常有 30-45 天延迟
    "fred":        3 * 86400,     # FRED 日度序列, 周末/假日允许延迟
}


def _get_store() -> DataStore:
    return DataStore(DB_PATH)


def _env_value(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    env_path = Path(".env")
    if not env_path.exists():
        return ""
    try:
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, val = raw.split("=", 1)
            if key.strip() == name:
                return val.strip().strip('"').strip("'")
    except Exception:
        return ""
    return ""


# ── Status: 查各表最新时间 ──────────────────────────────

def _get_latest_timestamp(
    store: DataStore,
    table: str,
    date_col: str,
    db_path: str | Path = DB_PATH,
) -> datetime | None:
    """查表中最新的时间戳"""
    try:
        with duckdb_readonly_connection(db_path, snapshot_first=True) as con:
            row = con.execute(f"SELECT MAX({date_col}) FROM {table}").fetchone()
            if row and row[0]:
                ts = datetime.fromisoformat(str(row[0]))
                return ts
    except Exception:
        return None
    return None


def _get_latest_release_at(table: str, date_col: str, db_path: str | Path = DB_PATH) -> float | None:
    try:
        with duckdb_readonly_connection(db_path, snapshot_first=True) as con:
            row = con.execute(
                f'SELECT release_at FROM "{table}" ORDER BY "{date_col}" DESC LIMIT 1'
            ).fetchone()
            if row and row[0]:
                return float(row[0])
    except Exception:
        return None
    return None


def _status_cot(store: DataStore) -> dict:
    """COT 最新数据日期"""
    latest = _get_latest_timestamp(store, "cot_gold", "report_date")
    stale = True
    if latest:
        age = (datetime.now() - latest).total_seconds()
        stale = age > FREQ["cot"]
    audit = latest_audit_by_source().get("cot", {})
    table_release_at = _get_latest_release_at("cot_gold", "report_date")
    return {
        "source": "cot",
        "table": "cot_gold",
        "latest": latest.strftime("%Y-%m-%d") if latest else "空表",
        "latest_effective_date": latest.strftime("%Y-%m-%d") if latest else None,
        "latest_release_at": table_release_at or audit.get("latest_release_at"),
        "last_refresh": audit.get("finished_at"),
        "status": audit.get("status"),
        "error": audit.get("error"),
        "stale": stale,
        "age_days": round((datetime.now() - latest).total_seconds() / 86400, 1) if latest else None,
    }


def _status_events(store: DataStore) -> dict:
    """events 表: 查最晚事件日期"""
    latest = _get_latest_timestamp(store, "events", "date", DUCKDB_EVENTS)
    stale = True
    if latest:
        age = (datetime.now() - latest).total_seconds()
        stale = age > FREQ["events"]
    audit = latest_audit_by_source().get("events", {})
    return {
        "source": "events",
        "table": "events",
        "latest": latest.strftime("%Y-%m-%d") if latest else "空表",
        "latest_effective_date": latest.strftime("%Y-%m-%d") if latest else None,
        "latest_release_at": None,
        "last_refresh": audit.get("finished_at"),
        "status": audit.get("status"),
        "error": audit.get("error"),
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
    audit = latest_audit_by_source().get("etf", {})
    table_release_at = _get_latest_release_at("etf_holdings", "date")
    return {
        "source": "etf",
        "table": "etf_holdings",
        "latest": latest.strftime("%Y-%m-%d") if latest else "空表",
        "latest_effective_date": latest.strftime("%Y-%m-%d") if latest else None,
        "latest_release_at": table_release_at or audit.get("latest_release_at"),
        "last_refresh": audit.get("finished_at"),
        "status": audit.get("status"),
        "error": audit.get("error"),
        "stale": stale,
        "age_days": round((datetime.now() - latest).total_seconds() / 86400, 1) if latest else None,
    }


def _status_fred(store: DataStore) -> dict:
    latest = _get_latest_timestamp(store, "macro_daily", "date")
    stale = True
    if latest:
        age = (datetime.now() - latest).total_seconds()
        stale = age > FREQ["fred"]
    audit = latest_audit_by_source().get("fred", {})
    table_release_at = _get_latest_release_at("macro_daily", "date")
    return {
        "source": "fred",
        "table": "macro_daily",
        "latest": latest.strftime("%Y-%m-%d") if latest else "空表",
        "latest_effective_date": latest.strftime("%Y-%m-%d") if latest else None,
        "latest_release_at": table_release_at or audit.get("latest_release_at"),
        "last_refresh": audit.get("finished_at"),
        "status": audit.get("status"),
        "error": audit.get("error"),
        "stale": stale,
        "age_days": round((datetime.now() - latest).total_seconds() / 86400, 1) if latest else None,
    }


def status_all(store: DataStore | None = None) -> list[dict]:
    return [
        _status_cot(store),
        _status_events(store),
        _status_etf(store),
        _status_fred(store),
    ]


# ── Refreshers ──────────────────────────────────────────

def refresh_cot(force: bool = False) -> bool:
    """拉取 CFTC COT 黄金持仓 (周度)"""
    run_id = start_refresh_audit("cot")
    store = _get_store()
    s = _status_cot(store)
    if not force and not s["stale"]:
        log.info(f"COT 未过期 (最新 {s['latest']}), skip")
        finish_refresh_audit(run_id, status="skipped", rows=0, latest_date=s.get("latest_effective_date"), latest_release_at=s.get("latest_release_at"))
        return True

    log.info("[COT] 从 CFTC 下载黄金持仓数据 ...")
    try:
        # 复用 load_cot_gold 的核心逻辑
        from scripts.load_cot_gold import download_year, parse_year
    except ImportError:
        log.error("无法 import scripts.load_cot_gold")
        finish_refresh_audit(run_id, status="failed", error="import scripts.load_cot_gold failed")
        return False

    current_year = datetime.now().year
    # 只刷最近 3 年 (2024-2026) — 旧数据已在库中
    years = list(range(current_year - 3, current_year + 1))
    all_dfs = []
    for y in years:
        try:
            zp = download_year(y, force_refresh=bool(force and y == current_year))
            record_raw_file("cot", zp, source_url=f"https://www.cftc.gov/files/dea/history/fut_disagg_txt_{y}.zip")
            df = parse_year(zp)
            if not df.empty:
                log.info(f"  {y}: {len(df)} rows")
                all_dfs.append(df)
        except Exception as e:
            log.warning(f"  {y}: {e}")
            continue

    if not all_dfs:
        log.error("COT: 无数据加载")
        finish_refresh_audit(run_id, status="failed", error="no data loaded")
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
    latest_date = df_all["report_date"].max().strftime("%Y-%m-%d")
    latest_release_at = cot_release_at(latest_date)
    finish_refresh_audit(run_id, status="success", rows=n, latest_date=latest_date, latest_release_at=latest_release_at)
    return True


def refresh_events(force: bool = False) -> bool:
    """拉取 ForexFactory 经济日历 (日度)"""
    run_id = start_refresh_audit("events")
    store = _get_store()
    s = _status_events(store)
    if not force and not s["stale"]:
        log.info(f"Events 未过期 (最新 {s['latest']}), skip")
        finish_refresh_audit(run_id, status="skipped", rows=0, latest_date=s.get("latest_effective_date"))
        return True

    try:
        from scripts.fetch_events_calendar import fetch_events, parse_and_filter, upsert_events
    except ImportError:
        log.error("无法 import fetch_events_calendar")
        finish_refresh_audit(run_id, status="failed", error="import fetch_events_calendar failed")
        return False

    log.info("[Events] 从 ForexFactory 拉取经济日历 ...")
    try:
        events = fetch_events(weeks=2)
        if not events:
            log.warning("Events: 返回空")
            finish_refresh_audit(run_id, status="failed", error="empty response")
            return False
        raw_dir = RAW_DIR / "events"
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_dir / f"forexfactory_{int(time.time())}.json"
        raw_path.write_text(json.dumps(events, ensure_ascii=False), encoding="utf-8")
        record_raw_file("events", raw_path, source_url="https://nfs.faireconomy.media/ff_calendar_thisweek.json")
        parsed = parse_and_filter(events)
        log.info(f"Events: 有效事件 {len(parsed)}/{len(events)} 条")
        if not parsed:
            log.warning("Events: 无有效事件 (非 USD 或被过滤)")
            finish_refresh_audit(run_id, status="failed", error="no parsed events")
            return False
        result = upsert_events(parsed)
        inserted = result.get("inserted", 0) or result.get("upserted", 0)
        log.info(f"Events: 入库 {inserted} 条")
        latest_date = max(ev["date"] for ev in parsed)
        finish_refresh_audit(run_id, status="success", rows=int(result.get("total", len(parsed)) or 0), latest_date=latest_date)
        return True
    except Exception as e:
        log.error(f"Events 刷新失败: {e}")
        finish_refresh_audit(run_id, status="failed", error=str(e)[:500])
        return False


def refresh_etf(force: bool = False) -> bool:
    """从 SEC EDGAR 拉 GLD 持仓 (季度)"""
    run_id = start_refresh_audit("etf")
    store = _get_store()
    s = _status_etf(store)
    if not force and not s["stale"]:
        log.info(f"ETF 未过期 (最新 {s['latest']}), skip")
        finish_refresh_audit(run_id, status="skipped", rows=0, latest_date=s.get("latest_effective_date"), latest_release_at=s.get("latest_release_at"))
        return True

    try:
        from scripts.load_gld_holdings_sec import main as gld_main
    except ImportError:
        log.error("无法 import load_gld_holdings_sec")
        finish_refresh_audit(run_id, status="failed", error="import load_gld_holdings_sec failed")
        return False

    log.info("[ETF] 从 SEC EDGAR 拉 GLD 持仓 ...")
    try:
        result = gld_main()
        log.info("ETF: GLD holdings 刷新完成")
        latest_date = result.get("latest_date") if isinstance(result, dict) else s.get("latest_effective_date")
        latest_release_at = result.get("latest_release_at") if isinstance(result, dict) else s.get("latest_release_at")
        rows = result.get("rows", 0) if isinstance(result, dict) else 0
        finish_refresh_audit(run_id, status="success", rows=rows, latest_date=latest_date, latest_release_at=latest_release_at)
        return True
    except Exception as e:
        log.error(f"ETF 刷新失败: {e}")
        finish_refresh_audit(run_id, status="failed", error=str(e)[:500])
        return False


def _fred_observations_url(series: str, api_key: str) -> str:
    query = urlencode(
        {
            "series_id": series,
            "api_key": api_key,
            "file_type": "json",
            "observation_start": "2000-01-01",
        }
    )
    return f"https://api.stlouisfed.org/fred/series/observations?{query}"


def refresh_fred(force: bool = False) -> bool:
    """Refresh FRED macro series into macro_daily.

    Missing QUANT_FRED_API_KEY is a clean skip so COT/ETF/events remain usable.
    """
    run_id = start_refresh_audit("fred")
    api_key = _env_value("QUANT_FRED_API_KEY")
    s = _status_fred(_get_store())
    if not api_key:
        log.warning("FRED skipped: QUANT_FRED_API_KEY not configured")
        finish_refresh_audit(run_id, status="skipped", rows=0, latest_date=s.get("latest_effective_date"), error="QUANT_FRED_API_KEY not configured")
        return True
    if not force and not s["stale"]:
        log.info(f"FRED 未过期 (最新 {s['latest']}), skip")
        finish_refresh_audit(run_id, status="skipped", rows=0, latest_date=s.get("latest_effective_date"), latest_release_at=s.get("latest_release_at"))
        return True

    total = 0
    latest_date: str | None = None
    latest_release: float | None = None
    try:
        raw_dir = RAW_DIR / "fred"
        raw_dir.mkdir(parents=True, exist_ok=True)
        fetched_at = time.time()
        for series in FRED_SERIES:
            url = _fred_observations_url(series, api_key)
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            payload = resp.json()
            raw_path = raw_dir / f"{series}.json"
            raw_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            record_raw_file("fred", raw_path, source_url=url.split("api_key=")[0] + "api_key=***")
            rows = payload.get("observations") or []
            batch = []
            for row in rows:
                date = str(row.get("date") or "")
                raw_value = row.get("value")
                if not date or raw_value in (None, ".", ""):
                    continue
                try:
                    value = float(raw_value)
                except Exception:
                    continue
                release_at = macro_release_at(date)
                batch.append((series, date, value, release_at, fetched_at, "fred"))
                if latest_date is None or date > latest_date:
                    latest_date = date
                    latest_release = release_at
            if batch:
                conn = connect_duckdb(DUCKDB_EXTERNAL)
                try:
                    conn.executemany(
                        """
                        INSERT OR REPLACE INTO macro_daily
                        (series, date, value, release_at, fetched_at, source)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        batch,
                    )
                finally:
                    conn.close()
            inserted = len(batch)
            total += inserted
            log.info("FRED %s: %s rows", series, inserted)
        finish_refresh_audit(run_id, status="success", rows=total, latest_date=latest_date, latest_release_at=latest_release)
        return True
    except Exception as e:
        log.error("FRED 刷新失败: %s", e)
        finish_refresh_audit(run_id, status="failed", rows=total, latest_date=latest_date, latest_release_at=latest_release, error=str(e)[:500])
        return False


# ── 主入口 ──────────────────────────────────────────────

SOURCES = {
    "cot": refresh_cot,
    "events": refresh_events,
    "etf": refresh_etf,
    "fred": refresh_fred,
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
    p.add_argument("--json", action="store_true", help="status 输出 JSON")
    p.add_argument("--once", action="store_true", help="cron 模式: 一次拉完退出")
    args = p.parse_args()

    if args.status:
        statuses = status_all()
        if args.json:
            print(json.dumps({"sources": statuses}, ensure_ascii=False, indent=2))
        else:
            print_status(statuses)
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


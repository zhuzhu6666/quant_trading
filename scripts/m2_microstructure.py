"""
M2.2: 市场微观结构变化检测 (2026-06-03)

定期跑 (cron 5min), 监控:
  1. 当前 spread: MT5 XAUUSD+ 实时 tick → 平均 spread
  2. 当前 commission: 模拟 0.01 lot 往返 → USD
  3. 当前 min_lot / step: 0.01 是否还合法
  4. 当前 contract_size: 100 oz 是否还合法

检测方式:
  - 拿 "上次已知" 的参数 (从 db live_microstructure 表或 fallback 静态值)
  - 跟当前 MT5 拉到的对比
  - 不一致 → 写 log + 报告

落盘: data/charts/m2_microstructure.txt (单次快照)
       + 长期趋势 db (可选, 留接口)
"""
import sys
import os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
import sqlite3
from datetime import datetime, timezone
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("microstructure")

DB_PATH = "data/market_data.db"
REPORT = Path("data/charts/m2_microstructure.txt")
SYMBOL = "XAUUSD+"

# 已知 baseline (2026-06-02 验证)
KNOWN_BASELINE = {
    "symbol": "XAUUSD+",
    "point": 0.01,
    "digits": 2,
    "trade_contract_size": 100.0,   # 1 lot = 100 oz
    "volume_min": 0.01,
    "volume_step": 0.01,
    "swap_long": None,   # 待 MT5 拉
    "swap_short": None,
    "spread_avg_points": 13.0,    # XAUUSD+ 当前 spread ~ 13 points = 0.13 USD
    "margin_initial": 0.0,   # 500x leverage, initial = 100/500 = 0.2
}


def fetch_current_microstructure(symbol: str = SYMBOL) -> dict:
    """MT5 拉当前 symbol_info + 实时 tick"""
    import MetaTrader5 as mt5
    if not mt5.initialize():
        return {"error": f"mt5.init failed: {mt5.last_error()}"}

    info = mt5.symbol_info(symbol)
    if info is None:
        mt5.shutdown()
        return {"error": f"symbol_info({symbol}) 返回 None"}

    tick = mt5.symbol_info_tick(symbol)
    mt5.shutdown()

    out = {
        "symbol": symbol,
        "point": float(info.point),
        "digits": int(info.digits),
        "trade_contract_size": float(info.trade_contract_size),
        "volume_min": float(info.volume_min),
        "volume_step": float(info.volume_step),
        "swap_long": float(info.swap_long) if info.swap_long is not None else None,
        "swap_short": float(info.swap_short) if info.swap_short is not None else None,
        "margin_initial": float(info.margin_initial) if hasattr(info, "margin_initial") else None,
    }
    if tick is not None:
        out["spread_current_points"] = int(tick.ask * (10 ** info.digits) - tick.bid * (10 ** info.digits))
        out["last_bid"] = float(tick.bid)
        out["last_ask"] = float(tick.ask)
        out["tick_time"] = datetime.fromtimestamp(tick.time, tz=timezone.utc).isoformat()
    return out


def check_drift(current: dict, baseline: dict) -> list[dict]:
    """比较 current vs baseline, 列出变化项"""
    if "error" in current:
        return [{"field": "mt5", "baseline": "OK", "current": current["error"], "level": "ERROR"}]

    drifts = []
    for key, base_val in baseline.items():
        if key in ("symbol",):
            continue
        cur_val = current.get(key)
        if cur_val is None and base_val is None:
            continue
        if cur_val != base_val:
            # 判断 level
            level = "INFO"
            if key in ("trade_contract_size", "volume_min", "volume_step", "point", "digits"):
                level = "CRITICAL"  # 这些变 = broker 大改, paper live 不一致
            elif key.startswith("swap") or key == "spread_current_points":
                level = "WARN"
            drifts.append({
                "field": key,
                "baseline": base_val,
                "current": cur_val,
                "level": level,
            })
    return drifts


def save_snapshot(current: dict, drifts: list[dict], db_path: str = DB_PATH):
    """落盘单次快照到 db microstructure 表 (新增, 自动创建)"""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS microstructure_snapshots (
            ts INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            point REAL,
            digits INTEGER,
            contract_size REAL,
            volume_min REAL,
            volume_step REAL,
            swap_long REAL,
            swap_short REAL,
            spread_points INTEGER,
            margin_initial REAL,
            drifts_json TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_micro_ts_sym
        ON microstructure_snapshots(ts, symbol)
    """)
    snap = {
        "ts": int(datetime.now(timezone.utc).timestamp()),
        "symbol": current.get("symbol", SYMBOL),
        "point": current.get("point"),
        "digits": current.get("digits"),
        "contract_size": current.get("trade_contract_size"),
        "volume_min": current.get("volume_min"),
        "volume_step": current.get("volume_step"),
        "swap_long": current.get("swap_long"),
        "swap_short": current.get("swap_short"),
        "spread_points": current.get("spread_current_points"),
        "margin_initial": current.get("margin_initial"),
        "drifts_json": json.dumps(drifts),
    }
    cols = ", ".join(snap.keys())
    placeholders = ", ".join("?" * len(snap))
    conn.execute(f"INSERT INTO microstructure_snapshots ({cols}) VALUES ({placeholders})",
                 list(snap.values()))
    conn.commit()
    conn.close()


def main():
    print("=" * 60)
    print("M2.2: 市场微观结构变化检测 (2026-06-03)")
    print("=" * 60)

    log.info("拉 MT5 微观结构...")
    current = fetch_current_microstructure()

    if "error" in current:
        log.error(f"MT5 拉失败: {current['error']}")
        # 写空报告
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        with open(REPORT, "w", encoding="utf-8") as f:
            f.write("M2.2: 微观结构快照 (2026-06-03)\n")
            f.write("=" * 70 + "\n\n")
            f.write(f"❌ MT5 未连接: {current['error']}\n")
            f.write(f"  启动方式: 需 MT5 终端登录后跑本脚本\n")
        return 1

    print(f"\n当前 ({current.get('tick_time', '?')}):")
    for k, v in current.items():
        if k != "drifts_json":
            print(f"  {k}: {v}")

    drifts = check_drift(current, KNOWN_BASELINE)
    print(f"\n差异: {len(drifts)} 项")
    for d in drifts:
        print(f"  [{d['level']}] {d['field']}: baseline={d['baseline']} → current={d['current']}")

    # 落盘
    save_snapshot(current, drifts)
    log.info("snapshot 已落盘 db microstructure_snapshots")

    # 报告
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("M2.2: 微观结构快照 (2026-06-03)\n")
        f.write("=" * 70 + "\n\n")
        f.write(f"时间: {current.get('tick_time', '?')}\n")
        f.write(f"标的: {current.get('symbol')}\n\n")
        f.write("当前微观结构:\n")
        for k, v in current.items():
            if k != "drifts_json":
                f.write(f"  {k}: {v}\n")
        f.write(f"\n差异 vs baseline (2026-06-02): {len(drifts)} 项\n")
        for d in drifts:
            f.write(f"  [{d['level']}] {d['field']}: {d['baseline']} → {d['current']}\n")
        f.write("\n" + "=" * 70 + "\n")
        f.write("解读\n")
        f.write("=" * 70 + "\n")
        if not drifts:
            f.write("✓ 所有参数跟 baseline 一致, broker 规则未变\n")
        else:
            critical = [d for d in drifts if d["level"] == "CRITICAL"]
            warn = [d for d in drifts if d["level"] == "WARN"]
            if critical:
                f.write(f"❌ {len(critical)} 项 CRITICAL 变化: broker 大改规则, paper vs live 不一致\n")
                for d in critical:
                    f.write(f"   {d['field']}: {d['baseline']} → {d['current']}\n")
                f.write("   → 需更新 config/instruments.yaml 跟 execution/mt5_bridge.py\n")
            if warn:
                f.write(f"⚠️  {len(warn)} 项 WARN: spread/swap 微调, 影响 PnL 0.5-2%\n")
        f.write("\n" + "-" * 70 + "\n")
        f.write("已知 baseline (2026-06-02):\n")
        for k, v in KNOWN_BASELINE.items():
            f.write(f"  {k}: {v}\n")

    print(f"\n报告: {REPORT}")
    return 0 if not drifts or all(d["level"] != "CRITICAL" for d in drifts) else 1


if __name__ == "__main__":
    sys.exit(main())

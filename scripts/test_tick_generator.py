"""scripts/test_tick_generator.py — TickGenerator 验证

用 1000 根 M15 bar 生成 100K tick, 校验 CSV 完整性.
"""
from __future__ import annotations

import csv
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.tick_generator import TickGenerator


def load_bars_from_csv(csv_path: str, max_bars: int = 1000) -> list[dict]:
    """从 CSV 加载 OHLCV bar, 返回 bar dict 列表."""
    bars: list[dict] = []
    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            if i >= max_bars:
                break
            # CSV time = "YYYY-MM-DD HH:MM:SS"
            ts = datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S")
            bars.append({
                "time": ts.timestamp(),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row.get("tick_volume", 0)),
            })
    return bars


def main():
    print("=" * 78)
    print("  TickGenerator — Tick-level 历史数据生成器 验证")
    print("=" * 78)

    # 1) 加载 bar 数据
    csv_path = "data/XAUUSD_M15.csv"
    print(f"\n[1] Loading bars from {csv_path} ...")
    bars = load_bars_from_csv(csv_path, max_bars=1000)
    print(f"    Loaded {len(bars)} M15 bars")

    # 统计数据用于后续校验
    bar_low_min = min(b["low"] for b in bars)
    bar_high_max = max(b["high"] for b in bars)
    bar_volume_sum = sum(b["volume"] for b in bars)
    print(f"    Bar low min:  {bar_low_min:.2f}")
    print(f"    Bar high max: {bar_high_max:.2f}")
    print(f"    Bar volume sum: {bar_volume_sum:.0f}")

    # 2) 创建生成器 + 生成 tick
    print(f"\n[2] Generating ticks via TickGenerator ...")
    gen = TickGenerator(config={
        "n_ticks_per_bar": 100,
        "seed": 42,
        "output_dir": "data/ticks",
        "symbol": "XAUUSD+",
        "bar_duration_seconds": 900,
    })
    t0 = time.time()
    filename = "XAUUSD+_M15_ticks.csv"
    filepath = gen.generate_and_save(bars, filename)
    elapsed = time.time() - t0
    print(f"    Done in {elapsed*1000:.1f} ms")
    print(f"    Saved to: {filepath}")

    # 3) 验证 CSV
    print(f"\n[3] Validating CSV ...")
    errors: list[str] = []

    # 3a) 文件存在
    p = Path(filepath)
    assert p.exists(), f"File not found: {filepath}"
    print(f"    ✓ File exists ({p.stat().st_size:,} bytes)")

    # 3b) 行数
    with open(filepath, "r") as f:
        lines = f.readlines()
    n_expected = len(bars) * 100  # 100 tick/bar
    n_data = len(lines) - 1      # -1 header
    print(f"    ✓ Lines: {len(lines)} (expected {n_expected + 1} = {n_expected} data + 1 header)")
    if n_data != n_expected:
        errors.append(f"Line count mismatch: got {n_data}, expected {n_expected}")

    # 3c) CSV header
    header = lines[0].strip()
    expected_header = "time,price,volume"
    print(f"    ✓ Header: \"{header}\" (expected \"{expected_header}\")")
    if header != expected_header:
        errors.append(f"Header mismatch: \"{header}\" != \"{expected_header}\"")

    # 3d) 解析 CSV 内容, 验证价格范围 + 体积累计
    reader = csv.DictReader(lines)
    tick_prices: list[float] = []
    tick_volume_sum = 0.0
    for row in reader:
        p_val = float(row["price"])
        v_val = float(row["volume"])
        tick_prices.append(p_val)
        tick_volume_sum += v_val

    tick_min = min(tick_prices)
    tick_max = max(tick_prices)
    print(f"    ✓ Tick price range: [{tick_min:.2f}, {tick_max:.2f}]")
    print(f"      Bar price envelope: [{bar_low_min:.2f}, {bar_high_max:.2f}]")

    if tick_min < bar_low_min - 0.02:
        errors.append(f"Tick min {tick_min:.2f} < bar low min {bar_low_min:.2f}")
    elif tick_min < bar_low_min:
        print(f"      ⚠  Tick min {tick_min:.2f} slightly below bar low min {bar_low_min:.2f} (within noise)")
    else:
        print(f"      ✓ Tick min >= bar low min")

    if tick_max > bar_high_max + 0.02:
        errors.append(f"Tick max {tick_max:.2f} > bar high max {bar_high_max:.2f}")
    elif tick_max > bar_high_max:
        print(f"      ⚠  Tick max {tick_max:.2f} slightly above bar high max {bar_high_max:.2f} (within noise)")
    else:
        print(f"      ✓ Tick max <= bar high max")

    # 3e) 体积累计
    volume_diff = abs(tick_volume_sum - bar_volume_sum)
    print(f"    ✓ Tick volume sum: {tick_volume_sum:.2f}")
    print(f"      Bar volume sum:  {bar_volume_sum:.2f}")
    print(f"      Difference: {volume_diff:.6f}")
    if volume_diff > 0.1:
        errors.append(f"Volume sum mismatch: tick={tick_volume_sum:.2f}, bar={bar_volume_sum:.2f}")
    else:
        print(f"      ✓ Volume sum matches (within rounding)")

    # 汇总
    print()
    print("=" * 78)
    if errors:
        print(f"  ❌ Validation FAILED — {len(errors)} error(s):")
        for e in errors:
            print(f"     • {e}")
    else:
        print("  ✅ TickGenerator 验证通过 — 全部检查 OK")
    print("=" * 78)

    # 统计摘要
    print()
    print("  === Stats ===")
    print(f"    Input bars:        {len(bars)}")
    print(f"    Generated ticks:   {n_data}")
    print(f"    File path:         {filepath}")
    print(f"    Price range:       [{tick_min:.2f}, {tick_max:.2f}]")
    print(f"    Volume sum (tick): {tick_volume_sum:.2f}")
    print(f"    Volume sum (bar):  {bar_volume_sum:.2f}")
    print(f"    Generation time:   {elapsed*1000:.1f} ms")

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()

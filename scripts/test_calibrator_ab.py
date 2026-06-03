"""
Test ProbabilityCalibrator A/B (calibrator on vs off)
=====================================================

Two-part verification:
  PART 1: Synthetic — load real calibrator, apply to model confidence
          distribution, show before/after weights and predicted WR.
  PART 2: End-to-end — MAB paper 3000 bar, calibrator ON vs OFF.

Usage:
    python scripts/test_calibrator_ab.py
"""
import os
import sys
import time
from pathlib import Path

ROOT = r'C:\Users\zhu\quant_trading'
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

import logging
import numpy as np
logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

from alpha.probability_calibrator import ProbabilityCalibrator
from alpha.persistent_registry import restore_from_log

CAL_PATH = Path("data/charts/calibrator_bucket.json")

# 4 M15 arms
M15_ARMS = ["multi_factor_m15", "trend_following", "mean_reversion", "breakout"]
BASELINE_WR = {
    "multi_factor_m15": (376, 362),  # W, L
    "trend_following":  (18, 30),
    "mean_reversion":   (457, 786),
    "breakout":         (530, 792),
}


def part1_synthetic():
    print()
    print("=" * 72)
    print("  PART 1: Synthetic — model confidence vs calibrated WR")
    print("=" * 72)
    if not CAL_PATH.exists():
        print(f"  ! SKIP: {CAL_PATH} not found")
        return None
    real = ProbabilityCalibrator.load(str(CAL_PATH))
    identity = ProbabilityCalibrator.identity()
    if real.method == "identity":
        print("  ! Real calibrator is identity, no-op")
        return None

    np.random.seed(42)
    n = 2000
    raw_probs = np.clip(np.random.beta(2, 2, n) * 0.6 + 0.2, 0.05, 0.95)
    raw_mean = float(np.mean(raw_probs))
    cal_probs = np.array([real.calibrate(float(p)) for p in raw_probs])
    cal_mean = float(np.mean(cal_probs))

    print(f"  Raw model confidence:  mean={raw_mean:.4f} (model claims {raw_mean*100:.1f}% WR)")
    print(f"  After calibration:     mean={cal_mean:.4f} (calibrated to {cal_mean*100:.1f}%)")
    print(f"  Calibrator method:     {real.method} ({len(real.buckets)} buckets)")
    print(f"  Net correction:        {cal_mean-raw_mean:+.4f} (negative = model overconfident)")

    print()
    print(f"  {'raw_p':>8} {'cal_p':>8} {'delta':>8}  说明")
    print("  " + "-" * 60)
    for p in [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]:
        c = real.calibrate(p)
        d = c - p
        if d > 0.02:
            note = "上修 (model 保守)"
        elif d < -0.02:
            note = "下修 (model 过于自信)"
        else:
            note = "持平"
        print(f"  {p:>8.3f} {c:>8.4f} {d:>+8.4f}  {note}")

    # Key insight: 0.6-0.7 bucket 校准后 0.5892, 0.7-0.8 桶 0.6, 0.8-0.9 桶 1.0
    # 0.9+ 不在 calibrator 数据中, 仍是 0.9
    # 综合: 在 0.6-0.8 区间 model 偏自信, 校准会下修 confidence, MAB 加权时该区间信号权重降
    return real


def _build_runner(calibrator, store, start_str, balance=500.0, enable_circuit=False):
    from strategy.registry import strategy_registry
    from strategy.mab_router import MABRouter
    from execution.mab_paper_runner import MABPaperRunner
    import strategies  # noqa: F401

    strategy_objs = {
        name: strategy_registry.create(
            name, symbol="XAUUSD+", timeframe="M15",
            sl_atr=3.0, tp_atr=4.0, cooldown_bars=3,
            enable_nfp_skip=True, nfp_skip_days=1,
            enable_dual_event_skip=True,
            enable_gvz_gate=True, gvz_drop_pct=-2.0,
        )
        for name in M15_ARMS
    }
    router = MABRouter(M15_ARMS, baseline=BASELINE_WR)
    runner = MABPaperRunner(
        strategies=strategy_objs,
        router=router,
        initial_balance=balance,
        enable_circuit=enable_circuit,
        calibrator=calibrator,
    )
    runner.paper.load_data(store, "XAUUSD+", "M15", start=start_str)
    return runner, strategy_objs


def part2_endtoend():
    print()
    print("=" * 72)
    print("  PART 2: End-to-end MAB paper 3000 bar, calibrator ON vs OFF")
    print("=" * 72)

    try:
        n = restore_from_log(verbose=False)
        print(f"  [T15.5] restored {n} shadow/discovered factors")
    except Exception as e:
        print(f"  [T15.5] restore failed: {e}")

    from data.store import DataStore
    store = DataStore('data/market_data.db')
    df = store.load_bars('XAUUSD+', 'M15')
    if df.empty:
        print("  ! no data")
        return
    n_bars = 3000
    start_ts = df.index[-n_bars].timestamp()
    start_str = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(start_ts))
    print(f"  Start: {start_str} (UTC), n_bars={n_bars}")

    cal_off = ProbabilityCalibrator.identity()
    cal_on = ProbabilityCalibrator.load(str(CAL_PATH)) if CAL_PATH.exists() else ProbabilityCalibrator.identity()
    print(f"  Cal OFF: {cal_off.method}")
    print(f"  Cal ON:  {cal_on.method} (buckets={len(cal_on.buckets)})")

    results = {}
    for label, cal in [('A no_cal', cal_off), ('B real_cal', cal_on)]:
        print(f"\n  --- {label} ---")
        t0 = time.time()
        runner, _ = _build_runner(cal, store, start_str)
        report = runner.run()
        elapsed = time.time() - t0
        results[label] = report
        print(f"  {label}: {report.total_trades}t / {report.total_return_pct:+.2f}% / "
              f"Sharpe {report.sharpe:.3f} / DD {report.max_drawdown_pct:.2f}% / "
              f"PF {report.profit_factor:.3f} / WR {report.win_rate:.1f}% / ${report.final_balance:.2f}  "
              f"({elapsed:.1f}s)")

    if all(k in results for k in ['A no_cal', 'B real_cal']):
        ra, rb = results['A no_cal'], results['B real_cal']
        print()
        print("  " + "=" * 60)
        print(f"  {'metric':<22} {'A no_cal':>14} {'B real_cal':>14} {'delta':>10}")
        print("  " + "-" * 60)
        for attr in ['total_trades', 'total_return_pct', 'sharpe', 'max_drawdown_pct',
                     'win_rate', 'profit_factor', 'final_balance']:
            va = getattr(ra, attr, 0)
            vb = getattr(rb, attr, 0)
            print(f"  {attr:<22} {va:>14.4f} {vb:>14.4f} {vb-va:>+10.4f}")


def main():
    part1_synthetic()
    part2_endtoend()
    print()
    print("=" * 72)
    print("  ALL DONE")
    print("=" * 72)


if __name__ == "__main__":
    main()
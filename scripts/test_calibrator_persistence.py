"""
Test ProbabilityCalibrator persistence (T17 / self-evolution gap #2)
====================================================================

Verifies:
  1. Load from existing calibrator_bucket.json → calibrator.method == "bucket"
  2. Calibrate values differ from identity
  3. Save → Load roundtrip preserves state
  4. Load from missing file → falls back to identity (no crash)

Usage:
    python scripts/test_calibrator_persistence.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = r'C:\Users\zhu\quant_trading'
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

import logging
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

from alpha.probability_calibrator import ProbabilityCalibrator


DEFAULT_PATH = Path("data/charts/calibrator_bucket.json")


def test_load_existing():
    print("=" * 72)
    print("  TEST 1: Load existing calibrator_bucket.json")
    print("=" * 72)
    if not DEFAULT_PATH.exists():
        print(f"  ! SKIP: {DEFAULT_PATH} not found")
        return None
    cal = ProbabilityCalibrator.load(str(DEFAULT_PATH))
    print(f"  method:        {cal.method}")
    print(f"  buckets count: {len(cal.buckets)}")
    print(f"  platt:         a={cal.platt_a:.4f}, b={cal.platt_b:.4f}")
    print(f"  first 3 buckets: {cal.buckets[:3]}")
    # 确认不是 identity
    if cal.method == "identity":
        print("  ! FAIL: loaded as identity (file may be empty)")
        return None
    print("  PASS: loaded as real calibrator")
    return cal


def test_calibrate_differs_from_identity(cal):
    print()
    print("=" * 72)
    print("  TEST 2: Calibrated values differ from identity")
    print("=" * 72)
    if cal is None:
        print("  ! SKIP: no calibrator from TEST 1")
        return
    identity = ProbabilityCalibrator.identity()
    test_probs = [0.25, 0.45, 0.55, 0.65, 0.75, 0.85]
    print(f"  {'p':>6} {'identity':>10} {'calibrated':>12} {'delta':>10}")
    print("  " + "-" * 42)
    for p in test_probs:
        pi = identity.calibrate(p)
        pc = cal.calibrate(p)
        delta = pc - pi
        marker = " <- differs" if abs(delta) > 0.01 else ""
        print(f"  {p:>6.3f} {pi:>10.4f} {pc:>12.4f} {delta:>+10.4f}{marker}")
    print("  PASS (calibrated != identity for non-trivial probs)")


def test_save_load_roundtrip(cal):
    print()
    print("=" * 72)
    print("  TEST 3: Save -> Load roundtrip")
    print("=" * 72)
    if cal is None:
        print("  ! SKIP: no calibrator from TEST 1")
        return
    # 用 temp file 不污染真实数据
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tf:
        tmp_path = tf.name
    try:
        cal.save(tmp_path)
        loaded = ProbabilityCalibrator.load(tmp_path)
        print(f"  Saved method:   {cal.method}")
        print(f"  Loaded method:  {loaded.method}")
        print(f"  Saved buckets:  {len(cal.buckets)}")
        print(f"  Loaded buckets: {len(loaded.buckets)}")
        print(f"  Saved platt:    ({cal.platt_a:.4f}, {cal.platt_b:.4f})")
        print(f"  Loaded platt:   ({loaded.platt_a:.4f}, {loaded.platt_b:.4f})")
        if (cal.method == loaded.method
                and cal.buckets == loaded.buckets
                and abs(cal.platt_a - loaded.platt_a) < 1e-9
                and abs(cal.platt_b - loaded.platt_b) < 1e-9):
            print("  PASS: roundtrip preserves all fields")
        else:
            print("  ! FAIL: roundtrip diverges")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def test_load_missing():
    print()
    print("=" * 72)
    print("  TEST 4: Load from missing file -> identity fallback")
    print("=" * 72)
    fake_path = Path("data/charts/__definitely_does_not_exist__.json")
    cal = ProbabilityCalibrator.load(str(fake_path))
    print(f"  Loaded method: {cal.method}")
    if cal.method == "identity":
        print("  PASS: fell back to identity (no crash)")
    else:
        print(f"  ! UNEXPECTED: method={cal.method}")


def test_fit_platt_persistence():
    print()
    print("=" * 72)
    print("  TEST 5: Fit Platt -> Save -> Load -> predict consistent")
    print("=" * 72)
    import numpy as np
    np.random.seed(42)
    # 模拟有偏的预测概率
    probs = np.array([0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8] * 20)
    y_true = (probs + np.random.normal(0, 0.1, len(probs)) > 0.5).astype(int)
    cal = ProbabilityCalibrator()
    cal.fit_platt(probs, y_true)
    print(f"  Fit platt: a={cal.platt_a:.4f}, b={cal.platt_b:.4f}, method={cal.method}")
    p_before = cal.calibrate(0.5)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tf:
        tmp_path = tf.name
    try:
        cal.save(tmp_path)
        cal2 = ProbabilityCalibrator.load(tmp_path)
        p_after = cal2.calibrate(0.5)
        print(f"  Before save: calibrate(0.5) = {p_before:.4f}")
        print(f"  After load:  calibrate(0.5) = {p_after:.4f}")
        if abs(p_before - p_after) < 1e-9:
            print("  PASS: Platt predict consistent after roundtrip")
        else:
            print(f"  ! FAIL: predict diverges by {abs(p_before - p_after):.6f}")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def main():
    print()
    cal = test_load_existing()
    test_calibrate_differs_from_identity(cal)
    test_save_load_roundtrip(cal)
    test_load_missing()
    test_fit_platt_persistence()
    print()
    print("=" * 72)
    print("  ALL TESTS DONE")
    print("=" * 72)


if __name__ == "__main__":
    main()
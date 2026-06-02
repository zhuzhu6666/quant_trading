"""
scripts/test_probability_calibrator.py
======================================

P0-7 校准器端到端测试:
  1. 加载 P0-6 walkforward OOS 预测 (4177 bar, 2 fold)
  2. 用前 50% 拟合 Platt 参数 / 桶级表
  3. 后 50% 评估: 校准前 vs 校准后 Brier score / log-loss
  4. 落盘数据 + 控制台报告

Brier score: mean((p - y)^2), 越低越好, 0=完美
Log-loss: -mean(y·log p + (1-y)·log(1-p)), 越低越好

完美校准: 预测 p 的样本里, 实际胜率应 ≈ p
"""
import sys
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from live.meta_learner_monitor import MetaLearnerMonitor
from alpha.probability_calibrator import ProbabilityCalibrator


def brier_score(y_true, p_pred):
    return float(np.mean((p_pred - y_true) ** 2))


def log_loss(y_true, p_pred, eps=1e-9):
    p_pred = np.clip(p_pred, eps, 1 - eps)
    return float(-np.mean(y_true * np.log(p_pred) + (1 - y_true) * np.log(1 - p_pred)))


def main():
    print("=" * 78)
    print("  P0-7 校准器端到端测试 — 加载 P0-6 OOS 预测, 拟合/评估校准器")
    print("=" * 78)

    # 1) 加载 P0-6 OOS
    inp = Path("data/charts/walkforward_oos.npy")
    if not inp.exists():
        print(f"  ⚠ {inp} 不存在, 请先跑 walkforward_p0_6.py")
        return
    data = np.load(inp, allow_pickle=True).item()
    y = data["y"]
    prob = data["prob"]
    print(f"加载 P0-6 OOS: n={len(y)} bar")
    print(f"  y mean = {y.mean():.4f}  (base rate)")
    print(f"  prob mean = {prob.mean():.4f}  (avg predicted prob)")

    # 2) 切分: 前 50% 拟合, 后 50% 评估
    split = len(y) // 2
    y_fit, y_eval = y[:split], y[split:]
    prob_fit, prob_eval = prob[:split], prob[split:]
    print(f"\n  拟合集: {len(y_fit)} bar,  评估集: {len(y_eval)} bar")

    # 3) 校准前 baseline
    brier_raw = brier_score(y_eval, prob_eval)
    logloss_raw = log_loss(y_eval, prob_eval)
    print(f"\n  校准前 (raw prob):")
    print(f"    Brier = {brier_raw:.4f}")
    print(f"    LogLoss = {logloss_raw:.4f}")

    # 4) 桶级校准 (用 monitor 模拟)
    monitor = MetaLearnerMonitor(model_names=["xgb"], window=len(y_fit), min_obs=30)
    for i in range(len(y_fit)):
        monitor.on_observation("xgb", float(prob_fit[i]), int(y_fit[i]))
    table = monitor.calibration_table("xgb", n_bins=10)
    print(f"\n  桶级校准表 (10 桶):")
    for row in table:
        print(f"    {row['bin']:<14s}  n={row['n']:>5d}  "
              f"avg_pred={row['avg_pred']:.4f}  actual_wr={row['actual_wr']:.4f}")

    cal_bucket = ProbabilityCalibrator.from_calibration_table(table, method="bucket")
    prob_bucket = cal_bucket.calibrate_array(prob_eval)
    brier_bucket = brier_score(y_eval, prob_bucket)
    logloss_bucket = log_loss(y_eval, prob_bucket)
    print(f"\n  校准后 (bucket 10 桶):")
    print(f"    Brier = {brier_bucket:.4f}  (改善 {brier_raw - brier_bucket:+.4f})")
    print(f"    LogLoss = {logloss_bucket:.4f}  (改善 {logloss_raw - logloss_bucket:+.4f})")

    # 5) Platt 缩放
    cal_platt = ProbabilityCalibrator.identity()
    cal_platt.fit_platt(prob_fit, y_fit)
    prob_platt = cal_platt.calibrate_array(prob_eval)
    brier_platt = brier_score(y_eval, prob_platt)
    logloss_platt = log_loss(y_eval, prob_platt)
    print(f"\n  校准后 (Platt scaling, a={cal_platt.platt_a:.4f}, b={cal_platt.platt_b:.4f}):")
    print(f"    Brier = {brier_platt:.4f}  (改善 {brier_raw - brier_platt:+.4f})")
    print(f"    LogLoss = {logloss_platt:.4f}  (改善 {logloss_raw - logloss_platt:+.4f})")

    # 6) 选最优方法
    methods = {
        "raw": (brier_raw, logloss_raw),
        "bucket": (brier_bucket, logloss_bucket),
        "platt": (brier_platt, logloss_platt),
    }
    best = min(methods.items(), key=lambda x: x[1][0])  # 按 Brier 选
    print(f"\n  最优方法: {best[0]}  (Brier {best[1][0]:.4f}, LogLoss {best[1][1]:.4f})")

    # 7) 落盘
    cal_bucket.save("data/charts/calibrator_bucket.json")
    cal_platt.save("data/charts/calibrator_platt.json")
    print(f"\n→ 落盘: data/charts/calibrator_bucket.json")
    print(f"→ 落盘: data/charts/calibrator_platt.json")

    # 8) 报告
    out = Path("data/charts/calibrator_report.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write("P0-7 概率校准器测试报告\n\n")
        f.write(f"输入: {inp} ({len(y)} bar)\n")
        f.write(f"拟合集: {len(y_fit)} bar, 评估集: {len(y_eval)} bar\n\n")
        f.write(f"桶级校准表 (10 桶):\n")
        for row in table:
            f.write(f"  {row['bin']}  n={row['n']}  avg_pred={row['avg_pred']:.4f}  "
                    f"actual_wr={row['actual_wr']:.4f}\n")
        f.write(f"\n校准效果对比:\n")
        f.write(f"  raw      Brier={brier_raw:.4f}  LogLoss={logloss_raw:.4f}\n")
        f.write(f"  bucket   Brier={brier_bucket:.4f}  LogLoss={logloss_bucket:.4f}  "
                f"Brier 改善 {brier_raw - brier_bucket:+.4f}\n")
        f.write(f"  platt    Brier={brier_platt:.4f}  LogLoss={logloss_platt:.4f}  "
                f"Brier 改善 {brier_raw - brier_platt:+.4f}\n")
        f.write(f"\n最优方法: {best[0]}\n")
        f.write(f"\n注: 把 calibrator_*.json 喂给 WeightedScorer.score() 前调 calibrate_signal_confidence() 即可\n")
    print(f"→ 落盘: {out}")

    # 9) 集成示例
    print("\n" + "=" * 78)
    print("  跟 WeightedScorer 集成示例")
    print("=" * 78)
    print("""
# 加载
from alpha.probability_calibrator import ProbabilityCalibrator, calibrate_signal_confidence
cal = ProbabilityCalibrator.load("data/charts/calibrator_platt.json")

# paper 链路里, 在 WeightedScorer.score() 之前:
for sig in signals:
    calibrate_signal_confidence(sig, cal)
# 之后 sig.confidence 已经是校准后的值, scoring 用的就是矫正后的概率
""")
    print("=" * 78)


if __name__ == "__main__":
    main()

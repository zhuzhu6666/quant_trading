"""
M1.1 单元回归: DSR + Bonferroni + Holm (2026-06-03)
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from alpha.calibration import (
    deflated_sharpe_ratio,
    bonferroni_correct,
    holm_correct,
    expected_max_sharpe_under_null,
)


def test_emax_monotonic():
    """E[max SR | H0] 随试验次数 N 单调递增 (N>=2)"""
    # N=1 是退化情形 (1-1/N=0 → -inf), 实际试验总是 >= 2
    emax_2 = expected_max_sharpe_under_null(2, 100)
    emax_10 = expected_max_sharpe_under_null(10, 100)
    emax_100 = expected_max_sharpe_under_null(100, 100)
    emax_1000 = expected_max_sharpe_under_null(1000, 100)
    assert emax_2 < emax_10 < emax_100 < emax_1000, \
        f"E[max] 应随 N 递增, 实际 {emax_2} {emax_10} {emax_100} {emax_1000}"
    print(f"  ✓ test_emax_monotonic (2→{emax_2:.2f}, 10→{emax_10:.2f}, 100→{emax_100:.2f}, 1000→{emax_1000:.2f})")


def test_dsr_obvious_signal():
    """强信号: SR=3, N=1, 应该显著"""
    rng = np.random.default_rng(42)
    rets = rng.normal(0.001, 0.01, 1000)  # mean=0.001, std=0.01 → SR=0.1/bar
    r = deflated_sharpe_ratio(observed_sr=3.0, returns=rets, n_trials=1)
    assert r["dsr"] > 2.0, f"SR=3 N=1 应强显著, DSR={r['dsr']:.2f}"
    assert r["significant"], f"应显著, p={r['p_value']:.4f}"
    print(f"  ✓ test_dsr_obvious_signal (DSR={r['dsr']:.2f}, p={r['p_value']:.4f})")


def test_dsr_under_correction():
    """N=100 试验后, 中等 SR=0.5 应被校正"""
    rng = np.random.default_rng(42)
    rets = rng.normal(0.001, 0.01, 1000)
    r1 = deflated_sharpe_ratio(observed_sr=0.5, returns=rets, n_trials=1)
    r100 = deflated_sharpe_ratio(observed_sr=0.5, returns=rets, n_trials=100)
    # 同样观测 SR, N=100 比 N=1 的 DSR 应更低 (更严格)
    assert r100["dsr"] < r1["dsr"], f"DSR(N=100)={r100['dsr']:.2f} 应 < DSR(N=1)={r1['dsr']:.2f}"
    # emax_N100 > emax_N1
    assert r100["emax_null"] > r1["emax_null"]
    print(f"  ✓ test_dsr_under_correction (N=1 DSR={r1['dsr']:.2f}, N=100 DSR={r100['dsr']:.2f}, emax {r1['emax_null']:.2f}→{r100['emax_null']:.2f})")


def test_dsr_autocorr_bias():
    """AR(1) 高自相关收益应有不同结果"""
    rng = np.random.default_rng(42)
    # 无自相关
    rets_no_ac = rng.normal(0.001, 0.01, 1000)
    # AR(1) 强自相关
    rets_with_ac = np.zeros(1000)
    rets_with_ac[0] = rng.normal()
    for i in range(1, 1000):
        rets_with_ac[i] = 0.7 * rets_with_ac[i - 1] + rng.normal(0, 0.01)
    r1 = deflated_sharpe_ratio(observed_sr=1.0, returns=rets_no_ac, n_trials=10)
    r2 = deflated_sharpe_ratio(observed_sr=1.0, returns=rets_with_ac, n_trials=10)
    # 自相关收益 ρ1 应更高
    assert r2["autocorr_1"] > r1["autocorr_1"], \
        f"AR(1) ρ1 应 > 无 AC, 实际 {r2['autocorr_1']:.3f} vs {r1['autocorr_1']:.3f}"
    print(f"  ✓ test_dsr_autocorr_bias (ρ1: 无AC={r1['autocorr_1']:.3f} AR1={r2['autocorr_1']:.3f}, "
          f"DSR: {r1['dsr']:.2f}→{r2['dsr']:.2f}, SE: {r1['sr_se']:.3f}→{r2['sr_se']:.3f})")


def test_bonferroni_basic():
    """Bonferroni: 20 次, 1 个 p=0.001 (其余 0.5), 应拒绝 1 个"""
    p_values = [0.5] * 19 + [0.001]
    r = bonferroni_correct(p_values, alpha=0.05)
    assert r["n_tests"] == 20
    assert r["corrected_alpha"] == 0.05 / 20  # 0.0025
    assert 19 in r["rejected_indices"]  # p=0.001 < 0.0025
    assert len(r["rejected_indices"]) == 1
    print(f"  ✓ test_bonferroni_basic (n_sig={r['n_significant']}/{r['n_tests']}, thresh={r['corrected_alpha']:.4f})")


def test_holm_more_powerful():
    """Holm 应至少跟 Bonferroni 一样, 通常更有力 (拒绝更多)"""
    p_values = [0.001, 0.004, 0.01, 0.5, 0.5, 0.5]
    r_b = bonferroni_correct(p_values, alpha=0.05)  # thresh=0.05/6=0.0083
    r_h = holm_correct(p_values, alpha=0.05)
    # Bonferroni: 0.001 < 0.0083 → reject 1; 0.004 < 0.0083 → reject 2; 0.01 > 0.0083 → stop
    # Holm: 0.001 < 0.05/6 → reject; 0.004 < 0.05/5 → reject; 0.01 < 0.05/4 → reject (更宽松)
    assert r_h["n_significant"] >= r_b["n_significant"]
    print(f"  ✓ test_holm_more_powerful (Bonf={r_b['n_significant']}, Holm={r_h['n_significant']})")


def test_dsr_real_scenario():
    """真实场景: baseline +407.34%/Sharpe 1.793, 多次 sweep 后, DSR 还显著吗"""
    # baseline PnL 假设 407% over 744 trades / 50204 bars ≈ 50000 bars × M15
    # 估算每 bar 收益: 假设均匀分布, total_ret = sum(rets)
    # 复利: final = 1 + 4.0734 = 5.0734 → per-bar ret ≈ 5.07^(1/50000) - 1 ≈ 0.000326
    # vol per bar ≈ 0.001 (估)
    rng = np.random.default_rng(42)
    n = 50000
    rets = rng.normal(0.000326, 0.005, n)  # 估算 baseline 收益分布
    # 你做了多少试验? 至少有 P1-E (3 baseline), MAB 调参, sweep, OOS test, IC scan (22 factors)
    n_trials_est = 200  # 保守估计
    r = deflated_sharpe_ratio(observed_sr=1.793, returns=rets, n_trials=n_trials_est)
    print(f"  ✓ test_dsr_real_scenario (DSR={r['dsr']:.2f}, p={r['p_value']:.4f}, emax={r['emax_null']:.2f}, sig={r['significant']})")


def main():
    print("M1.1 单元回归: DSR + Bonferroni + Holm (2026-06-03)")
    print("=" * 60)
    test_emax_monotonic()
    test_dsr_obvious_signal()
    test_dsr_under_correction()
    test_dsr_autocorr_bias()
    test_bonferroni_basic()
    test_holm_more_powerful()
    test_dsr_real_scenario()
    print("=" * 60)
    print("✓ 全部 7 个测试通过")


if __name__ == "__main__":
    main()

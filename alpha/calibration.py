"""
Deflated Sharpe Ratio (DSR) — Bailey & López de Prado, 2014
多次试验后, 校正"观测 Sharpe"的偏倚.

核心思想:
  - 跑 N 次策略/参数试验, 看到最大 Sharpe = max(SR_i), 但期望 E[max] > E[SR]
  - DSR 给出"在 N 次试验下, 观测到的 SR 是否统计显著 > 0"

公式 (简化版, 单组实验):
  DSR = (SR_obs - SR_benchmark) / sqrt(V[SR])
  其中 V[SR] 包含:
    - 1) 收益自相关偏倚
    - 2) 试验次数 N 的多重检验偏倚 (E[max SR under H0])
    - 3) 非正态 (skew/kurtosis) 偏倚

参考: Bailey, Borwein, López de Prado, Zhu (2014)
"Pseudoscience in Financial Markets"
"""
import math
import numpy as np
from scipy import stats


def expected_max_sharpe_under_null(n_trials: int, n_obs: int, skew: float = 0.0, kurt: float = 3.0) -> float:
    """
    H0 下 N 次试验 max|SR| 的期望 (近似公式).
    Bailey et al. 2014 论文 Eq. (5):

      E[max SR] ≈ (1 - γ) * Φ^(-1)(1 - 1/N) + γ * Φ^(-1)(1 - 1/(N*e))
      + skew * ((1-γ) * ((1-1/N))^2 - γ * ((1-1/(N*e))^2 - 1/(N*e))) / 4
      - kurt_excess * (γ * ((1-1/(N*e))^3 - 1/(3*N*e)) - (1-γ)/3) / 6

    γ ≈ 0.5772156649 (Euler-Mascheroni)
    """
    euler = 0.5772156649
    gamma = euler

    if n_trials <= 0 or n_obs <= 2:
        return 0.0

    # 1/N 和 1/(N*e) 的分位数
    a = 1.0 - 1.0 / n_trials
    b = 1.0 - 1.0 / (n_trials * math.e)
    try:
        z_a = stats.norm.ppf(a)
        z_b = stats.norm.ppf(b)
    except Exception:
        return 0.0

    emax = (1 - gamma) * z_a + gamma * z_b

    # 偏度修正
    emax += skew * ((1 - gamma) * (a ** 2) - gamma * (b ** 2 - 1.0 / (n_trials * math.e))) / 4.0

    # 峰度修正 (kurtosis excess = kurt - 3)
    kurt_excess = kurt - 3.0
    emax -= kurt_excess * (gamma * (b ** 3 - 1.0 / (3.0 * n_trials * math.e)) - (1 - gamma) / 3.0) / 6.0

    return emax


def deflated_sharpe_ratio(
    observed_sr: float,
    returns: np.ndarray | None = None,
    n_trials: int = 1,
    sr_benchmark: float = 0.0,
) -> dict:
    """
    算 DSR.

    Args:
        observed_sr: 观测到的 Sharpe Ratio (年化或非年化, 一致即可)
        returns: 真实收益率序列 (period 收益率, 非累计). 用来算 skew/kurtosis/自相关.
                 None 时用正态假设 (skew=0, kurt=3)
        n_trials: 试验总次数 (包括未公布的). 越大越严格.
        sr_benchmark: 对比的基准 SR, 默认 0.

    Returns:
        dict: {dsr, p_value, sr_observed, sr_benchmark,
               sr_std, emax_null, significant}
    """
    n_obs = len(returns) if returns is not None else 0
    n_trials = max(n_trials, 2)  # N=1 退化 (1-1/N=0 → -inf), 至少 2 才有意义

    if returns is not None and len(returns) >= 4:
        # 算收益分布的统计量
        skew = float(stats.skew(returns))
        kurt = float(stats.kurtosis(returns, fisher=True)) + 3.0  # Fisher=False 拿真实峰度

        # 1) SR 标准误 (无自相关时)
        sr_std = 1.0 / math.sqrt(n_obs)
        if sr_std == 0 or not np.isfinite(sr_std):
            sr_std = 1e-9

        # 2) 自相关偏倚: 收益率若正自相关, SR 估偏高
        if n_obs >= 4:
            rho1 = float(np.corrcoef(returns[:-1], returns[1:])[0, 1])
        else:
            rho1 = 0.0
        sr_var = (1.0 / (n_obs - 1)) * (
            1
            - skew * observed_sr
            + ((kurt - 1) / 4.0) * (observed_sr ** 2)
            + 2 * rho1 * observed_sr ** 2  # 自相关项
        )
        if sr_var <= 0:
            sr_var = sr_std ** 2
        sr_se = math.sqrt(sr_var)
    else:
        skew = 0.0
        kurt = 3.0
        rho1 = 0.0
        sr_se = 1.0 / math.sqrt(max(n_obs, 4))

    # 3) 多重检验偏倚: E[max SR | H0]
    emax = expected_max_sharpe_under_null(n_trials, max(n_obs, 4), skew, kurt)
    # DSR = (SR_obs - max(SR_benchmark, E[max|H0])) / SR_se
    sr_threshold = max(sr_benchmark, emax)
    dsr = (observed_sr - sr_threshold) / sr_se if sr_se > 0 else 0.0

    # p-value (单边, 检验 DSR > 0)
    p_value = 1.0 - stats.norm.cdf(dsr) if np.isfinite(dsr) else 1.0

    return {
        "dsr": float(dsr),
        "p_value": float(p_value),
        "significant": bool(p_value < 0.05),
        "sr_observed": float(observed_sr),
        "sr_benchmark": float(sr_benchmark),
        "sr_se": float(sr_se),
        "emax_null": float(emax),
        "skew": float(skew),
        "kurt": float(kurt),
        "autocorr_1": float(rho1),
        "n_obs": int(n_obs),
        "n_trials": int(n_trials),
    }


def bonferroni_correct(p_values: list[float], alpha: float = 0.05) -> dict:
    """
    Bonferroni 多重检验校正: 拒绝 H0 当 p_i < alpha / N.

    Returns: {n_tests, n_significant, corrected_alpha, rejected_indices}
    """
    n = len(p_values)
    if n == 0:
        return {"n_tests": 0, "n_significant": 0, "corrected_alpha": alpha, "rejected_indices": []}
    corrected = alpha / n
    rejected = [i for i, p in enumerate(p_values) if p < corrected]
    return {
        "n_tests": n,
        "n_significant": len(rejected),
        "corrected_alpha": float(corrected),
        "rejected_indices": rejected,
    }


def holm_correct(p_values: list[float], alpha: float = 0.05) -> dict:
    """
    Holm-Bonferroni 阶梯校正 (比 Bonferroni 更有力, 但仍控制 FWER).
    排序 p, 拒绝 H0_i 当 p_(i) < alpha / (N - i + 1).
    """
    n = len(p_values)
    if n == 0:
        return {"n_tests": 0, "n_significant": 0, "rejected_indices": []}
    indexed = sorted(enumerate(p_values), key=lambda x: x[1])
    rejected = set()
    last_significant_i = -1
    for rank, (orig_idx, p) in enumerate(indexed):
        thresh = alpha / (n - rank)
        if p < thresh:
            rejected.add(orig_idx)
            last_significant_i = rank
        else:
            break  # 阶梯: 后续都不显著
    return {
        "n_tests": n,
        "n_significant": len(rejected),
        "rejected_indices": sorted(rejected),
    }

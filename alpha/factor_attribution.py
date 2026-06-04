"""alpha/factor_attribution.py — 因子去重 / 边际 IC 归因

给定 (T, N) 因子矩阵 + (T,) forward_returns:
  - compute_ic_matrix: 每因子 vs fwd 的 IC (corr) 统计
  - correlation_matrix: 因子两两 Pearson 相关
  - marginal_ic: 每次去掉一个因子, 算剩余的回归拟合 IC; marg = full - leave_k_out
  - redundancy_report: 找 |corr| > threshold 的高相关对
  - recommend_drops: 边际 IC 小 + 高相关 → 可删

依赖: numpy + pandas + scipy.stats (已有)
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd
from scipy import stats

logger = logging.getLogger(__name__)


class FactorAttribution:
    def __init__(self, factor_names: list[str],
                 factor_returns: np.ndarray,
                 forward_returns: np.ndarray,
                 ic_window: int = 500):
        if factor_returns.ndim != 2:
            raise ValueError("factor_returns 必须 2D (T, N)")
        if factor_returns.shape[0] != len(forward_returns):
            raise ValueError("factor_returns 行数 != forward_returns 长度")
        if factor_returns.shape[1] != len(factor_names):
            raise ValueError("factor_returns 列数 != factor_names 长度")
        self.factor_names = list(factor_names)
        self.factor_returns = factor_returns
        self.forward_returns = forward_returns
        self.ic_window = ic_window

    # ── IC 矩阵 ──
    def compute_ic_matrix(self) -> pd.DataFrame:
        """每因子 vs forward_returns 的 IC 统计 (rolling window 收敛到全样本)."""
        rows = []
        for i, name in enumerate(self.factor_names):
            x = self.factor_returns[:, i]
            y = self.forward_returns
            mask = ~(np.isnan(x) | np.isnan(y))
            x_, y_ = x[mask], y[mask]
            n = len(x_)
            if n < 30:
                rows.append({"factor": name, "ic_mean": 0, "ic_std": 0, "ic_ir": 0,
                             "t_stat": 0, "n_obs": n, "abs_ic": 0, "active": False})
                continue
            r, p = stats.pearsonr(x_, y_)
            t_stat = r * np.sqrt((n - 2) / (1 - r ** 2 + 1e-12))
            rows.append({
                "factor": name, "ic_mean": round(float(r), 4),
                "ic_std": round(float(np.std(x_) / (np.std(y_) + 1e-12) * 0.1), 4),
                "ic_ir": round(float(r / (np.std(x_) / (np.std(y_) + 1e-12) + 1e-12) * 0.1), 4),
                "t_stat": round(float(t_stat), 3), "n_obs": n,
                "abs_ic": round(float(abs(r)), 4), "active": abs(r) >= 0.02,
            })
        return pd.DataFrame(rows)

    # ── 相关矩阵 ──
    def correlation_matrix(self) -> pd.DataFrame:
        """因子两两 Pearson 相关."""
        df = pd.DataFrame(self.factor_returns, columns=self.factor_names)
        return df.corr().round(3)

    # ── 边际 IC ──
    def marginal_ic(self) -> pd.DataFrame:
        """去掉因子 k 后, 剩余回归拟合 IC - 全模型 IC = marginal contribution."""
        x_all = self.factor_returns
        y = self.forward_returns
        # 全模型
        mask_all = ~np.isnan(y)
        for i in range(x_all.shape[1]):
            mask_all &= ~np.isnan(x_all[:, i])
        x_full = x_all[mask_all]
        y_full = y[mask_all]
        # 用 lstsq (避免 sklearn)
        # BUG-22 (audit 2026-06-04): rcond=1e-10 截掉接近零的奇异值, 防共线炸 beta
        beta_full, _, _, _ = np.linalg.lstsq(x_full, y_full, rcond=1e-10)
        y_hat_full = x_full @ beta_full
        full_ic = float(np.corrcoef(y_full, y_hat_full)[0, 1])

        rows = []
        for k in range(len(self.factor_names)):
            mask = np.ones(x_all.shape[1], dtype=bool)
            mask[k] = False
            x_drop = x_all[mask_all][:, mask]
            try:
                # BUG-22 同上
                beta, _, _, _ = np.linalg.lstsq(x_drop, y_full, rcond=1e-10)
                y_hat = x_drop @ beta
                leave_ic = float(np.corrcoef(y_full, y_hat)[0, 1])
            except Exception:
                leave_ic = 0.0
            marg = full_ic - leave_ic
            rows.append({
                "factor": self.factor_names[k],
                "full_ic": round(full_ic, 4),
                "leave_k_out_ic": round(leave_ic, 4),
                "marginal_ic": round(marg, 4),
                "importance_pct": round(100 * marg / max(full_ic, 1e-9), 1),
            })
        return pd.DataFrame(rows)

    # ── 冗余报告 ──
    def redundancy_report(self, threshold: float = 0.7) -> list[str]:
        corr = self.correlation_matrix()
        msgs = []
        for i, a in enumerate(self.factor_names):
            for j, b in enumerate(self.factor_names):
                if j <= i:
                    continue
                c = corr.iloc[i, j]
                if abs(c) >= threshold:
                    msgs.append(f"{a} vs {b}: corr={c:.3f}")
        return msgs

    # ── 推荐删除 ──
    def recommend_drops(self, threshold: float = 0.7) -> list[str]:
        marg = self.marginal_ic()
        corr = self.correlation_matrix()
        msgs = []
        for i, fname in enumerate(self.factor_names):
            my_marg = marg.loc[marg["factor"] == fname, "marginal_ic"].iloc[0]
            # 该因子跟其它因子的最大 |corr|
            other_corrs = [abs(corr.iloc[i, j]) for j in range(len(self.factor_names)) if j != i]
            max_corr = max(other_corrs) if other_corrs else 0
            if my_marg < 0.001 and max_corr >= threshold:
                msgs.append(f"drop {fname}: marginal={my_marg:+.4f}, max_corr={max_corr:.3f}")
        return msgs

    # ── 全报告 ──
    def full_report(self) -> str:
        lines = []
        lines.append("=" * 78)
        lines.append("  FactorAttribution Full Report")
        lines.append("=" * 78)
        lines.append("")
        lines.append("─── IC Matrix ───")
        lines.append(self.compute_ic_matrix().to_string(index=False))
        lines.append("")
        lines.append("─── Correlation Matrix ───")
        lines.append(self.correlation_matrix().to_string())
        lines.append("")
        lines.append("─── Marginal IC ───")
        lines.append(self.marginal_ic().to_string(index=False))
        lines.append("")
        redun = self.redundancy_report()
        lines.append(f"─── Redundancy (|corr| > 0.7): {len(redun)} pair(s) ───")
        for m in redun:
            lines.append(f"  {m}")
        if not redun:
            lines.append("  (none)")
        lines.append("")
        drops = self.recommend_drops()
        lines.append(f"─── Recommended Drops: {len(drops)} ───")
        for m in drops:
            lines.append(f"  {m}")
        if not drops:
            lines.append("  (none)")
        lines.append("")
        lines.append("=" * 78)
        return "\n".join(lines)

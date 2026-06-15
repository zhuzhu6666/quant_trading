# TODO — 剩余工作（Phase 0-7 已全部完成）

Factor Takeover v4 全部 Phase (0~7) 已完成交付，详见 `docs/UPGRADE_BLUEPRINT.md`。
以下为剩余边缘项和技术债务。

---

## 🟡 延期项（蓝图明确标记 [~]）

| # | 项 | 原因 | 触发条件 |
|---|----|------|---------|
| D1 | Autoencoder 非线性特征压缩 | §12 过拟合风险，20K bar 极易学噪声 | bar > 100K |
| D2 | 数据版本控制系统 | DuckDB snapshot 已够用，无需额外系统 | 需要时重构 |

## 🔧 技术债务（v10 审计遗留，见 `PROJECT_AUDIT_v10.md`）

| # | 文件 | 问题 | 说明 |
|---|------|------|------|
| TD1 | `execution/ctrader_bridge.py:250` | spot price 除数 `10**5` 硬编码 XAUUSD | 换品种会价格错误 |
| TD2 | `execution/router.py:154-177` | on_fill 反向成交只 log 不处理 | 仓位漂移风险 |
| TD3 | `execution/market_impact.py:116` | 成本计算用 1.0 而非实际金价 ~$3000 | 成本低估 3000× |
| TD4 | 全项目 | `DataFrame.append` → `pd.concat` (~10-20 处) | Pandas 3.x 兼容 |
| TD5 | 前端 8+ 处 | polling 缺少 AbortController | 组件卸载后 setState 泄漏 |
| TD6 | 前端 | 死导入清理 (50+ 处) | 代码整洁 |

## 📋 已知技术债务（蓝图 Appendix C.1）

| # | 问题 | 说明 |
|---|------|------|
| 1 | `regime_classifier.py` 因子脱节 | 使用 `[aroon, cci, mfi, williams_r]` 不在 39 因子池 |
| 2 | GVZ gate stub | `_get_gvz_change()` 永久返回 None |
| 3 | `train_xgb_walkforward.py` 仅 4 因子 | 非蓝图 68 特征设计 |
| 4 | `scripts/factor_pca.py` docstring 过时 | 声称 15 因子，实际 39 |

> 以上不阻塞运行。当需要扩展 ML 或多品种时按优先级处理。

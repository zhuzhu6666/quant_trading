"""ExecutionGate — 开仓闸门。

组合了信号强度/冷却期/事件过滤器。

设计文档: docs/architecture.md
"""

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ── 已知 NFP 发布日期 (每月第一个周五) ──
# 自动生成: 每月首周五。此处预置已知日期加速查找,
# 运行时 fallback 到动态计算
_NFP_DATES: set[str] = {
    "2026-01-02", "2026-02-06", "2026-03-06", "2026-04-03",
    "2026-05-01", "2026-06-05", "2026-07-03", "2026-08-07",
    "2026-09-04", "2026-10-02", "2026-11-06", "2026-12-04",
    "2027-01-08", "2027-02-05", "2027-03-05", "2027-04-02",
}

# 已知节假日 (NFP 不会发布)
_HOLIDAYS: set[str] = {
    "2026-01-01",  # New Year
    "2026-04-03",  # Good Friday (already NFP, so no-op)
    "2026-12-25",  # Christmas
    "2027-01-01",
}


def _is_nfp_date(date_str: str) -> bool:
    """判断是否为 NFP 发布日期。"""
    if date_str in _HOLIDAYS:
        return False
    if date_str in _NFP_DATES:
        return True
    # fallback: 动态判断每月第一个周五 (排除节假日)
    try:
        from datetime import datetime
        d = datetime.strptime(date_str, "%Y-%m-%d")
        if d.weekday() == 4 and d.day <= 7:
            return True
    except Exception:
        pass
    return False


def _get_gvz_change(date_str: str) -> float | None:
    """获取指定日期 GVZ 变化率。

    从 data/news_cache 读取 GVZ 日度数据，计算当日相对前日变化 %。
    数据库不可用时返回 None（跳过检查）。
    """
    try:
        from data.news_cache import load_gvz_series, daily_change_pct
        series = load_gvz_series()
        return daily_change_pct(series, date_str)
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════
# GateResult
# ═══════════════════════════════════════════════════════════

@dataclass
class GateResult:
    """闸门过滤结果。"""
    passed: bool
    reason: str
    cooldown_remaining: int = 0


# ═══════════════════════════════════════════════════════════
# ExecutionGate
# ═══════════════════════════════════════════════════════════

class ExecutionGate:
    """开仓闸门。

    检查链: 信号强度 → 冷却期 → 事件

    Args:
        config: 字典，包含以下键:
            - signal_threshold: float (default 0.4)
            - cooldown_bars: int (default 3)
    """

    def __init__(self, config: dict[str, Any]):
        self._config = dict(config or {})
        self._cooldown_bars: int = 0
        self._threshold = float(self._config.get("signal_threshold", 0.4))
        self._cooldown_setting = int(self._config.get("cooldown_bars", 3))

    # ── 核心 ────────────────────────────────────────────

    def filter(
        self,
        composite: Any,
        factor_values: dict[str, float | None],
        bar: dict[str, Any],
        governor_state: Any = None,
    ) -> GateResult:
        """检查所有闸门.

        Args:
            composite: CompositeSignal（必须有 direction 和 score 属性）
            factor_values: 因子原始值
            bar: 当前 bar dict（含 time 等字段）
            governor_state: 可选, RiskGovernor 状态快照 (P3.2).

        Returns:
            GateResult(passed=True) 表示可以通过，否则为 False。
        """
        # P3.2: RiskGovernor 最高层裁决
        if governor_state is not None:
            try:
                from risk.governor import RiskGovernor
                gov = RiskGovernor.shared()
                verdict = gov.allow_trade(governor_state)
                if not verdict.allowed:
                    return GateResult(False, f"governor:{verdict.reason}")
            except Exception:
                pass
        # 1. 信号强度门槛
        if composite.direction == 0:
            return GateResult(False, "signal_below_threshold")
        # ★ 信号绝对值门槛 — 之前漏了这步, 弱信号也能通过
        if abs(composite.score) < self._threshold:
            return GateResult(False, "signal_below_threshold")

        # 2. 冷却期
        if self._cooldown_bars > 0:
            return GateResult(
                False, f"cooldown_{self._cooldown_bars}",
                cooldown_remaining=self._cooldown_bars,
            )

        # 3. 事件过滤器
        event_result = self._event_filter(composite.direction, bar)
        if not event_result.passed:
            return event_result

        # 5. 通过 → 设置冷却期
        self._cooldown_bars = self._cooldown_setting

        return GateResult(True, "passed")

    def _event_filter(self, direction: int, bar: dict[str, Any]) -> GateResult:
        """事件过滤器: NFP skip / FOMC boost / GVZ gate。"""
        cfg_enable_nfp = self._config.get(
            "strategy_enable_nfp_skip",
            self._config.get("risk_enable_nfp_skip", False),
        )
        cfg_enable_gvz = self._config.get(
            "strategy_enable_gvz_gate",
            self._config.get("risk_enable_gvz_gate", False),
        )
        cfg_gvz_threshold = self._config.get(
            "strategy_gvz_drop_pct",
            self._config.get("risk_gvz_drop_pct", -2.0),
        )

        bar_ts = bar.get("time", 0)
        bar_date = datetime.fromtimestamp(bar_ts, tz=timezone.utc).strftime("%Y-%m-%d")

        # NFP skip: NFP 发布日不开仓
        if cfg_enable_nfp and _is_nfp_date(bar_date):
            return GateResult(False, "nfp_skip")

        # GVZ gate: GVZ 暴跌时不开仓 (波动率异常)
        if cfg_enable_gvz:
            gvz_chg = _get_gvz_change(bar_date)
            if gvz_chg is not None and gvz_chg < cfg_gvz_threshold:
                return GateResult(False, "gvz_gate")

        return GateResult(True, "passed")

    def tick(self):
        """每根 bar 调用，减冷却计数。"""
        if self._cooldown_bars > 0:
            self._cooldown_bars -= 1

    # ── 重置 ────────────────────────────────────────────

    def reset(self):
        """重置冷却期（策略切换/重启时）。"""
        self._cooldown_bars = 0

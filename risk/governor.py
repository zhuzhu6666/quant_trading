"""risk/governor.py — 最高层风控裁决器 (RiskGovernor).

Governor 不是指标计算器, 而是决策仲裁者.
它回答: "系统现在能不能做某个动作?"

裁决类型:
  - allow_trade:           允许开仓?
  - allow_weight_update:   允许权重更新?
  - allow_promotion:       允许因子晋升?
  - allow_new_factor:      允许注册新因子?
  - force_dry_run:         强制只算信号不下单?
  - force_deleverage:       强制降低仓位?

Governor 单例, 可在任何地方用 RiskGovernor.shared() 访问.
"""

from __future__ import annotations

import logging
import time
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class GovernorVerdict:
    """裁决结果."""
    allowed: bool
    reason: str = ""
    suggestion: str = ""


@dataclass
class GovernorState:
    """Governor 决策时读取的外部状态快照."""
    # 账户状态
    balance: float = 0.0
    equity: float = 0.0
    peak_equity: float = 0.0
    drawdown_pct: float = 0.0
    # 交易状态
    open_positions: int = 0
    total_position_value: float = 0.0
    consecutive_losses: int = 0
    daily_trades: int = 0
    daily_loss_pct: float = 0.0
    # 风控指标 (来自现有模块)
    var_95: float = 0.0
    kelly_fraction: float = 1.0
    circuit_broken: bool = False
    # 系统状态
    loop_running: bool = True
    bridge_connected: bool = True
    data_lag_seconds: float = 0.0
    timeframe_seconds: int = 0
    seconds_since_last_trade: float = 0.0
    bars_since_last_trade: float = 0.0
    # 额外
    extra: dict[str, Any] = field(default_factory=dict)


class RiskGovernor:
    """最高层风控裁决器.

    用法:
        governor = RiskGovernor.shared()
        # 交易前
        verdict = governor.allow_trade(state_snapshot)
        if not verdict.allowed:
            logger.warning("trade blocked: %s", verdict.reason)
            return

    阈值可通过 RuntimeConfig 热更新.
    """

    _instance: RiskGovernor | None = None
    _lock = threading.Lock()

    def __init__(
        self,
        max_drawdown_pct: float = 15.0,        # 最大回撤 %
        max_consecutive_losses: int = 8,        # 连续亏损上限
        max_daily_loss_pct: float = 5.0,        # 日亏损上限
        max_daily_trades: int = 20,             # 日交易上限
        min_bridge_uptake: bool = True,         # 桥接断开时的应对
        data_lag_max_seconds: float = 3600.0,   # 数据最大延迟 (秒)
        loss_cooldown_after_losses: int = 2,    # 连续亏损达到 N 笔后进入冷静期
        loss_cooldown_bars: int = 3,            # 冷静期长度 (bar)
        circuit_breaker_bypass: bool = False,   # 是否绕过熔断
    ):
        self._cfg = {
            "max_drawdown_pct": max_drawdown_pct,
            "max_consecutive_losses": max_consecutive_losses,
            "max_daily_loss_pct": max_daily_loss_pct,
            "max_daily_trades": max_daily_trades,
            "min_bridge_uptake": min_bridge_uptake,
            "data_lag_max_seconds": data_lag_max_seconds,
            "loss_cooldown_after_losses": loss_cooldown_after_losses,
            "loss_cooldown_bars": loss_cooldown_bars,
            "circuit_breaker_bypass": circuit_breaker_bypass,
        }
        self._dry_run_mode: bool = False          # governor 强制 dry-run
        self._deleverage_pct: float = 0.0         # governor 强制降杠杆 %
        self._overrides: dict[str, bool] = {}     # 手动 override

    @classmethod
    def shared(cls) -> RiskGovernor:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """重置单例 (测试用)."""
        with cls._lock:
            cls._instance = None

    # ── 配置 ────────────────────────────────────────────────────────

    def update_config(self, **kwargs) -> None:
        self._cfg.update(kwargs)

    def set_override(self, key: str, value: bool) -> None:
        """手动覆盖裁决 (如 force_allow_trade=True)."""
        self._overrides[key] = value

    def clear_override(self, key: str) -> None:
        self._overrides.pop(key, None)

    # ── 裁决方法 ────────────────────────────────────────────────────

    def allow_trade(self, state: GovernorState | None = None) -> GovernorVerdict:
        """允许开新仓吗?"""
        if self._dry_run_mode or self._overrides.get("force_dry_run"):
            return GovernorVerdict(False, "force_dry_run", "governor forced dry-run")

        if state is None:
            return GovernorVerdict(True, "no_state")

        cfg = self._cfg

        # 熔断
        if state.circuit_broken and not cfg["circuit_breaker_bypass"]:
            return GovernorVerdict(False, "circuit_broken", "circuit breaker triggered")

        # live loop 未运行
        if not state.loop_running:
            return GovernorVerdict(False, "loop_not_running", "live loop is not running")

        # 桥接不可用
        if cfg["min_bridge_uptake"] and not state.bridge_connected:
            return GovernorVerdict(False, "bridge_disconnected", "broker bridge is disconnected")

        # 回撤超限
        if state.drawdown_pct >= cfg["max_drawdown_pct"]:
            return GovernorVerdict(False, "drawdown_too_high",
                                   f"drawdown {state.drawdown_pct:.1f}% >= {cfg['max_drawdown_pct']:.0f}%")

        # 连续亏损后的短冷静期
        cooldown_after_losses = int(state.extra.get("loss_cooldown_after_losses", cfg.get("loss_cooldown_after_losses", 0)) or 0)
        cooldown_bars = int(state.extra.get("loss_cooldown_bars", cfg.get("loss_cooldown_bars", 0)) or 0)
        timeframe_seconds = int(state.timeframe_seconds or 0)
        if (
            cooldown_after_losses > 0
            and cooldown_bars > 0
            and timeframe_seconds > 0
            and state.consecutive_losses >= cooldown_after_losses
        ):
            required_seconds = float(cooldown_bars * timeframe_seconds)
            elapsed_seconds = float(max(0.0, state.seconds_since_last_trade or 0.0))
            if elapsed_seconds < required_seconds:
                remaining_seconds = max(0.0, required_seconds - elapsed_seconds)
                remaining_bars = remaining_seconds / timeframe_seconds if timeframe_seconds > 0 else 0.0
                return GovernorVerdict(
                    False,
                    "loss_cooldown_active",
                    f"wait {remaining_seconds:.0f}s (~{remaining_bars:.1f} bars) after {state.consecutive_losses} losses",
                )

        # 连续亏损超限。若配置了冷静期，冷静期结束后允许系统重新试探；
        # 未配置冷静期时保留旧的硬拦截行为。
        if (
            state.consecutive_losses >= cfg["max_consecutive_losses"]
            and not (cooldown_after_losses > 0 and cooldown_bars > 0 and timeframe_seconds > 0)
        ):
            return GovernorVerdict(False, "consecutive_losses",
                                   f"{state.consecutive_losses} >= {cfg['max_consecutive_losses']}")

        # 日亏损超限
        if state.daily_loss_pct >= cfg["max_daily_loss_pct"]:
            return GovernorVerdict(False, "daily_loss_limit",
                                   f"daily loss {state.daily_loss_pct:.1f}% >= {cfg['max_daily_loss_pct']:.0f}%")

        # 日交易笔数超限
        if cfg["max_daily_trades"] > 0 and state.daily_trades >= cfg["max_daily_trades"]:
            return GovernorVerdict(False, "daily_trade_limit",
                                   f"{state.daily_trades} >= {cfg['max_daily_trades']}")

        # 数据延迟
        if state.data_lag_seconds > cfg["data_lag_max_seconds"]:
            return GovernorVerdict(False, "data_lag",
                                   f"data lag {state.data_lag_seconds:.0f}s > {cfg['data_lag_max_seconds']:.0f}s")

        runtime_health = state.extra.get("runtime_health", {}) if isinstance(state.extra, dict) else {}
        system_health = runtime_health.get("system_health", {}) if isinstance(runtime_health, dict) else {}
        component_status = system_health.get("component_status", {}) if isinstance(system_health, dict) else {}
        if system_health:
            disk_status = str(component_status.get("disk_space") or "")
            l2_status = str(component_status.get("l2_depth") or "")
            block_on_disk_critical = bool(state.extra.get("block_on_disk_critical", True))
            require_l2_depth = bool(state.extra.get("require_l2_depth", False))
            if block_on_disk_critical and disk_status == "critical":
                return GovernorVerdict(False, "disk_space_critical", "disk space is critically low")
            if require_l2_depth and l2_status == "critical":
                return GovernorVerdict(False, "l2_depth_unavailable", "required L2 depth feed is unavailable")

        return GovernorVerdict(True, "ok")

    def allow_weight_update(self, state: GovernorState | None = None) -> GovernorVerdict:
        """允许权重更新吗?"""
        if self._overrides.get("force_weight_freeze"):
            return GovernorVerdict(False, "force_weight_freeze", "governor froze weights")

        if state is None:
            return GovernorVerdict(True, "no_state")

        cfg = self._cfg

        # 回撤超限时冻结权重
        if state.drawdown_pct >= cfg["max_drawdown_pct"] * 0.8:  # 80% of max → freeze
            return GovernorVerdict(False, "drawdown_approaching_limit",
                                   f"drawdown {state.drawdown_pct:.1f}% >= {cfg['max_drawdown_pct']*0.8:.0f}%")

        return GovernorVerdict(True, "ok")

    def allow_template_switch(self, state: GovernorState | None = None) -> GovernorVerdict:
        """允许在线参数模板切换吗?"""
        if self._overrides.get("force_template_switch_freeze"):
            return GovernorVerdict(False, "force_template_switch_freeze")
        return self.allow_weight_update(state)

    def allow_factor_disable(self, state: GovernorState | None = None) -> GovernorVerdict:
        """允许禁用 live 因子吗?"""
        if self._overrides.get("force_factor_disable_freeze"):
            return GovernorVerdict(False, "force_factor_disable_freeze")
        return GovernorVerdict(True, "ok")

    def allow_factor_retire(self, state: GovernorState | None = None) -> GovernorVerdict:
        """允许退役因子吗?"""
        if self._overrides.get("force_factor_retire_freeze"):
            return GovernorVerdict(False, "force_factor_retire_freeze")
        return GovernorVerdict(True, "ok")

    def allow_factor_rollback(self, state: GovernorState | None = None) -> GovernorVerdict:
        """允许回滚自治因子动作吗?"""
        if self._overrides.get("force_factor_rollback_freeze"):
            return GovernorVerdict(False, "force_factor_rollback_freeze")
        return GovernorVerdict(True, "ok")

    def allow_context_policy(self, state: GovernorState | None = None) -> GovernorVerdict:
        """允许启用/调整 context 策略吗?"""
        if self._overrides.get("force_context_policy_freeze"):
            return GovernorVerdict(False, "force_context_policy_freeze")
        return GovernorVerdict(True, "ok")

    def allow_promotion(self, state: GovernorState | None = None) -> GovernorVerdict:
        """允许因子晋升吗?"""
        if self._overrides.get("force_promotion_freeze"):
            return GovernorVerdict(False, "force_promotion_freeze")

        if state is None:
            return GovernorVerdict(True, "no_state")

        cfg = self._cfg
        # 大幅回撤时暂停因子晋升 (进化暂停, 专注回本)
        if state.drawdown_pct >= cfg["max_drawdown_pct"] * 0.7:
            return GovernorVerdict(False, "drawdown_too_high_for_promotion")

        return GovernorVerdict(True, "ok")

    def allow_new_factor(self, state: GovernorState | None = None) -> GovernorVerdict:
        """允许注册新因子吗?"""
        if self._overrides.get("force_new_factor_freeze"):
            return GovernorVerdict(False, "force_factor_freeze")

        if state is None:
            return GovernorVerdict(True, "no_state")

        cfg = self._cfg
        if state.drawdown_pct >= cfg["max_drawdown_pct"] * 0.6:
            return GovernorVerdict(False, "drawdown_too_high_for_new_factor")

        return GovernorVerdict(True, "ok")

    def force_dry_run(self) -> bool:
        """Governor 强制 dry-run?"""
        return self._dry_run_mode or self._overrides.get("force_dry_run", False)

    def force_deleverage(self) -> float:
        """Governor 强制降杠杆比例 (0=不降, 0.5=降到50%)"""
        return self._deleverage_pct

    # ── 主动设置 ────────────────────────────────────────────────────

    def set_dry_run(self, enabled: bool) -> None:
        self._dry_run_mode = enabled
        logger.warning("[Governor] dry_run=%s (manual)", enabled)

    def set_deleverage(self, pct: float) -> None:
        self._deleverage_pct = max(0.0, min(1.0, pct))
        logger.warning("[Governor] deleverage=%.0f%% (manual)", pct * 100)

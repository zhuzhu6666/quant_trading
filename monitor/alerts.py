"""
Alerts — 告警系统

支持多渠道告警：
- Log（基础）
- 钉钉机器人（TODO）
- 微信企业号（TODO）

告警级别：
- INFO: 日常事件（开仓、平仓）
- WARNING: 风控预警（接近限额）
- CRITICAL: 熔断、异常
"""

import logging
from datetime import datetime
from enum import Enum

logger = logging.getLogger(__name__)


class AlertLevel(Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class AlertManager:
    """告警管理器"""

    def __init__(self, channels: list[str] | None = None):
        self.channels = channels or ["log"]

    def send(self, level: AlertLevel, title: str, message: str,
             extra: dict | None = None):
        """发送告警到所有启用的渠道"""
        for channel in self.channels:
            if channel == "log":
                self._send_log(level, title, message, extra)
            elif channel == "dingtalk":
                self._send_dingtalk(level, title, message, extra)
            elif channel == "wechat":
                self._send_wechat(level, title, message, extra)

    def _send_log(self, level: AlertLevel, title: str, message: str,
                  extra: dict | None = None):
        log_func = {
            AlertLevel.INFO: logger.info,
            AlertLevel.WARNING: logger.warning,
            AlertLevel.CRITICAL: logger.error,
        }[level]
        log_func(f"[{title}] {message}" + (f" | {extra}" if extra else ""))

    def _send_dingtalk(self, level: AlertLevel, title: str, message: str,
                       extra: dict | None = None):
        """TODO: 钉钉机器人webhook"""
        pass

    def _send_wechat(self, level: AlertLevel, title: str, message: str,
                     extra: dict | None = None):
        """TODO: 企业微信webhook"""
        pass

    # ── 快捷方法 ──

    def trade_opened(self, symbol: str, direction: str, price: float,
                     size: float, sl: float, tp: float):
        self.send(AlertLevel.INFO, "开仓",
                  f"{symbol} {direction} price={price:.2f} size={size} "
                  f"sl={sl:.2f} tp={tp:.2f}")

    def trade_closed(self, symbol: str, pnl: float, reason: str = ""):
        emoji = "✅" if pnl > 0 else "❌"
        self.send(AlertLevel.INFO, f"{emoji} 平仓",
                  f"{symbol} pnl=${pnl:.2f} {reason}")

    def risk_warning(self, message: str):
        self.send(AlertLevel.WARNING, "⚠️ 风控预警", message)

    def circuit_breaker(self, reason: str):
        self.send(AlertLevel.CRITICAL, "🔴 熔断", reason)

    def daily_summary(self):
        """日内汇总"""
        from core.state import state
        d = state.daily
        self.send(AlertLevel.INFO, "日内汇总",
                  f"交易{d.total_trades}笔 胜率{d.winning_trades}/{d.total_trades if d.total_trades else 1}="
                  f"{state.win_rate:.0f}% PnL=${d.net_pnl:.2f} "
                  f"最大回撤{d.max_drawdown_pct:.1f}%")


# 全局单例
alerts = AlertManager()

"""
Multi-channel Alerter — 多通道告警系统

支持通道:
- 钉钉 Webhook (POST JSON)
- 企微 Webhook (POST JSON)
- 本地日志文件 (append)
- 控制台彩色输出

告警级别 (由低到高): DEBUG < INFO < WARNING < ERROR < CRITICAL
min_level 过滤: 低于该级别的消息不发送。
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from typing import Any

try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]

# ── 级别常量 ──
DEBUG = "DEBUG"
INFO = "INFO"
WARNING = "WARNING"
ERROR = "ERROR"
CRITICAL = "CRITICAL"

LEVEL_ORDER = {DEBUG: 0, INFO: 1, WARNING: 2, ERROR: 3, CRITICAL: 4}

# ── 控制台 ANSI 颜色 (Windows >=10 / modern terminal OK) ──
_RESET = "\033[0m"
_COLORS: dict[str, str] = {
    "CRITICAL": "\033[1;31m",   # 亮红
    "ERROR": "\033[0;31m",      # 红
    "WARNING": "\033[0;33m",    # 黄
    "INFO": "\033[0;37m",       # 白
    "DEBUG": "\033[0;90m",      # 灰
}

# Windows 旧终端启用 ANSI
if sys.platform == "win32":
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass  # 不强制


def _should_send(min_level: str, level: str) -> bool:
    return LEVEL_ORDER.get(level, 0) >= LEVEL_ORDER.get(min_level, 0)


def _level_prefix(level: str) -> str:
    return f"[{level}]"


def _timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _format_content(level: str, title: str, message: str) -> str:
    return f"{_level_prefix(level)} {title}\n{message}"


class Alerter:
    """多通道告警器"""

    def __init__(self, config: dict[str, Any] | None = None):
        config = config or {}

        self.dingtalk_webhook: str | None = config.get("dingtalk_webhook")
        self.wecom_webhook: str | None = config.get("wecom_webhook")
        self.log_file: str | None = config.get("log_file")
        self.min_level: str = config.get("min_level", WARNING).upper()

        # 日志目录自动创建
        if self.log_file:
            d = os.path.dirname(self.log_file)
            if d:
                os.makedirs(d, exist_ok=True)

        self._logger = logging.getLogger("alerter")
        self._logger.setLevel(logging.DEBUG)

    # ── 公共发送入口 ──

    def send(self, level: str, title: str, message: str, **kwargs: Any) -> None:
        """
        发送告警。

        Parameters
        ----------
        level : str
            DEBUG / INFO / WARNING / ERROR / CRITICAL
        title : str
            短标题（如 "熔断触发"）
        message : str
            详细内容
        """
        if not _should_send(self.min_level, level):
            return

        content = _format_content(level, title, message)

        # 控制台（始终输出）
        self._console(level, content)

        # 本地日志文件
        if self.log_file:
            self._write_log(level, content)

        # 钉钉 Webhook
        if self.dingtalk_webhook:
            self._webhook_dingtalk(level, content)

        # 企微 Webhook
        if self.wecom_webhook:
            self._webhook_wecom(level, content)

    # ── 便捷方法 ──

    def circuit_tripped(self, breaker_name: str, reason: str, state: dict) -> None:
        """熔断器触发告警 (CRITICAL)"""
        msg = (
            f"Breaker : {breaker_name}\n"
            f"Reason  : {reason}\n"
            f"State   : {state}"
        )
        self.send(CRITICAL, "🔴 Circuit Breaker Tripped", msg, **state)

    def daily_loss(self, pct: float, threshold: float, balance: float) -> None:
        """日亏告警 (WARNING / ERROR)"""
        level = ERROR if pct >= threshold else WARNING
        msg = (
            f"Drawdown : {pct:.2f}%\n"
            f"Threshold: {threshold:.2f}%\n"
            f"Balance  : ${balance:.2f}"
        )
        self.send(level, f"📉 Daily Loss {pct:.1f}%", msg)

    def trade_closed(self, strategy: str, pnl: float, symbol: str) -> None:
        """交易关闭告警 (INFO)"""
        emoji = "✅" if pnl >= 0 else "❌"
        msg = (
            f"Strategy : {strategy}\n"
            f"Symbol   : {symbol}\n"
            f"PnL      : ${pnl:.2f}"
        )
        self.send(INFO, f"{emoji} Trade Closed {symbol}", msg)

    # ── 自检 ──

    def test(self) -> None:
        """自检：验证各通道是否正常（不传真实 webhook 则只测本地日志 + 控制台）"""
        print("\n" + "=" * 50)
        print("  Alerter Self-Test")
        print("=" * 50)
        print(f"  min_level       : {self.min_level}")
        print(f"  dingtalk_webhook: {'set' if self.dingtalk_webhook else 'not set'}")
        print(f"  wecom_webhook   : {'set' if self.wecom_webhook else 'not set'}")
        print(f"  log_file        : {self.log_file}")
        print("=" * 50)

        msgs = [
            (DEBUG, "Debug Test", "This is a debug message"),
            (INFO, "Info Test", "This is an info message"),
            (WARNING, "Warning Test", "This is a warning message"),
            (ERROR, "Error Test", "This is an error message"),
            (CRITICAL, "Critical Test", "This is a critical message"),
        ]
        for level, title, msg in msgs:
            self.send(level, title, msg)

        print("=" * 50)
        print("  Self-Test Complete")
        print("=" * 50)

    # ── 内部通道 ──

    def _console(self, level: str, content: str) -> None:
        color = _COLORS.get(level, _RESET)
        ts = _timestamp()
        line = f"{color}{ts} | {content}{_RESET}"
        print(line)

    def _write_log(self, level: str, content: str) -> None:
        ts = _timestamp()
        line = f"{ts} | {content}\n"
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:  # type: ignore[arg-type]
                f.write(line)
        except OSError as e:
            print(f"WARN: write log failed — {e}", file=sys.stderr)

    def _webhook_dingtalk(self, level: str, content: str) -> None:
        if requests is None:
            print("WARN: 'requests' not installed, skip dingtalk webhook", file=sys.stderr)
            return
        url = self.dingtalk_webhook
        if not url:
            return
        payload = {"msgtype": "text", "text": {"content": content}}
        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
        except Exception as e:
            print(f"WARN: dingtalk webhook failed — {e}", file=sys.stderr)

    def _webhook_wecom(self, level: str, content: str) -> None:
        if requests is None:
            print("WARN: 'requests' not installed, skip wecom webhook", file=sys.stderr)
            return
        url = self.wecom_webhook
        if not url:
            return
        payload = {"msgtype": "text", "text": {"content": content}}
        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
        except Exception as e:
            print(f"WARN: wecom webhook failed — {e}", file=sys.stderr)


if __name__ == "__main__":
    # 命令行自检
    a = Alerter({"log_file": "logs/alerts.log", "min_level": "DEBUG"})
    a.test()

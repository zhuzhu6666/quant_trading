"""
Strategy Base — 策略基类

所有策略继承此类，实现:
- on_bar(bar): 接收K线，生成信号
- 自动接入EventBus
- 自动参数注册
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

from core.event_bus import bus, Event, EventType

logger = logging.getLogger(__name__)


class SignalType(Enum):
    NONE = 0
    LONG = 1
    SHORT = -1
    CLOSE = 2


@dataclass
class Signal:
    """交易信号"""
    strategy: str
    symbol: str
    direction: int          # 1=long, -1=short, 0=flat
    strength: float = 1.0   # 信号强度 0-1
    sl_atr: float = 0.0     # SL乘数
    tp_atr: float = 0.0     # TP乘数
    atr: float = 0.0        # 当前ATR值
    price: float = 0.0      # 信号触发价
    timestamp: float = 0.0
    meta: dict | None = None
    # ── Phase 7 扩展字段 (全部 Optional, 向后兼容) ──
    factor_scores: dict[str, float] | None = None  # 因子名 → 数值 (e.g. {"rsi": 28.5, "adx": 31.2})
    regime: dict[str, bool] | None = None          # regime 标签 (e.g. {"TRENDING_UP": True, "RANGING": False, "HIGH_VOL": True})
    confidence: float | None = None                # 0-1, 模型/投票置信度


class BaseStrategy(ABC):
    """
    策略基类

    子类必须实现:
    - on_bar(bar) → Signal | None

    可选覆写:
    - on_init(): 策略初始化
    - on_tick(tick): tick级信号
    """

    params: dict = {}  # 子类覆盖

    def __init__(self, name: str, symbol: str, timeframe: str = "H1"):
        self.name = name
        self.symbol = symbol
        self.timeframe = timeframe
        self._ready = False  # 指标预热完成
        self._bar_count = 0
        self._cooldown = 0
        # 指标快照（子类应在 on_bar 末尾更新）
        self.last_atr: float | None = None
        self.last_indicators: dict = {}

    def on_init(self):
        """策略初始化（子类覆写）"""
        pass

    @abstractmethod
    def on_bar(self, bar: dict) -> Signal | None:
        """
        处理一根K线，返回信号或None

        bar字段: open, high, low, close, volume, time, timeframe, complete
        """
        ...

    def on_tick(self, tick: dict) -> Signal | None:
        """tick级信号（可选覆写，默认不实现）"""
        return None

    def publish_signal(self, signal: Signal):
        """发布信号到EventBus"""
        bus.publish_sync(Event(
            type=EventType.SIGNAL,
            data=signal,
            timestamp=signal.timestamp,
            source=self.name,
        ))

    def __repr__(self):
        return f"{self.name}({self.symbol}/{self.timeframe})"

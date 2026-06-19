"""
execution/base.py — 统一经纪商抽象接口 + 公共数据类型

Phase 4: BaseBrokerBridge 定义了所有执行通道 (cTrader / Paper / MT5) 的统一接口。
所有 bridge 实现返回此模块定义的统一类型 (OrderResult / PositionInfo / AccountInfo)。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pandas as pd


# ── 统一数据类型 ──────────────────────────────────────────

@dataclass
class OrderResult:
    """统一订单结果 — 所有 Bridge 返回此类型"""
    success: bool
    order_id: int = 0
    position_id: int = 0
    error_code: str = ""
    comment: str = ""
    price: float = 0.0
    volume: float = 0.0


@dataclass
class PositionInfo:
    """统一持仓信息"""
    position_id: int = 0
    symbol: str = ""
    direction: int = 0       # 1=long, -1=short, 0=flat
    volume: float = 0.0
    entry_price: float = 0.0
    current_price: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    pnl: float = 0.0
    commission: float = 0.0
    swap: float = 0.0


@dataclass
class AccountInfo:
    """统一账户信息"""
    balance: float = 0.0
    equity: float = 0.0
    margin: float = 0.0
    margin_free: float = 0.0
    margin_level: float = 0.0
    leverage: float = 0.0
    currency: str = "USD"
    account_id: int = 0
    name: str = ""


# ── 抽象接口 ─────────────────────────────────────────────

class BaseBrokerBridge(ABC):
    """统一经纪商接口

    所有执行通道 (cTrader / Paper / MT5) 实现此接口。
    Paper 和 Live 模式的区别仅在于注入的 bridge 实现不同，
    上层调用代码完全一致。
    """

    @abstractmethod
    def connect(self) -> bool:
        """建立连接，返回是否成功"""
        ...

    @abstractmethod
    def disconnect(self) -> None:
        """断开连接"""
        ...

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """是否已连接"""
        ...

    @abstractmethod
    def market_buy(self, symbol: str, volume: float,
                   sl: float = 0.0, tp: float = 0.0,
                   comment: str = "") -> OrderResult:
        """市价买入"""
        ...

    @abstractmethod
    def market_sell(self, symbol: str, volume: float,
                    sl: float = 0.0, tp: float = 0.0,
                    comment: str = "") -> OrderResult:
        """市价卖出"""
        ...

    @abstractmethod
    def close_position(self, position_id: int,
                       volume: float = 0.0) -> OrderResult:
        """平仓 (volume=0 表示全平)"""
        ...

    @abstractmethod
    def get_positions(self, symbol: str = "") -> list[PositionInfo]:
        """获取当前持仓列表 (symbol="" 表示所有品种)"""
        ...

    @abstractmethod
    def account_info(self) -> AccountInfo:
        """获取账户信息"""
        ...

    # ── 可选方法 (提供默认实现) ──────────────────────────

    def amend_sl_tp(self, position_id: int, sl: float, tp: float) -> bool:
        """修改持仓的 SL/TP — 可选，默认返回 False"""
        return False

    def get_spot_price(self, symbol: str = "") -> float | None:
        """获取实时报价 — 可选，默认返回 None"""
        return None

    def fetch_bars(self, symbol: str, timeframe: str,
                   n_bars: int) -> "pd.DataFrame | None":
        """拉取历史 K 线 — 可选，默认返回 None"""
        return None

    def has_token(self) -> bool:
        """检查凭证是否已设置 — 可选，默认 True"""
        return True

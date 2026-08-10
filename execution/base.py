"""
execution/base.py — 统一经纪商抽象接口 + 公共数据类型

该模块定义统一经纪商接口与公共类型（OrderResult / PositionInfo / AccountInfo）。
当前实盘主链使用 execution/ctrader_bridge（经 live_service -> risk/policy_service ->
execution/ctrader_bridge -> execution/deal_sync -> backend/ledger/service）。
Paper / MT5 实现留作历史兼容和测试，不作为默认主链执行。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Mapping

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
    # Additive compatibility field.  cTrader v2 always fills this with one of
    # confirmed/rejected/unknown/simulated; legacy bridges may leave it empty.
    outcome: str = ""
    intent_id: str = ""
    client_order_id: str = ""
    client_msg_id: str = ""


@dataclass
class PositionInfo:
    """统一持仓信息

    volume 使用 API 原生口径（cTrader volume unit），上层不要再自行
    解释成 volume/手；如需展示，交给 UI 层格式化。
    """
    position_id: int = 0
    symbol_id: int = 0
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
    open_timestamp: float = 0.0  # epoch seconds
    # Additive component truth.  Empty means a legacy producer that did not
    # publish component state; the cTrader reconcile/event paths always fill
    # these fields explicitly.  In particular, ``pnl == 0`` is only a known
    # flat PnL when ``pnl_state == "known"``.
    current_price_state: str = ""
    current_price_source: str = ""
    current_price_observed_at: float = 0.0
    current_price_reason_code: str = ""
    pnl_state: str = ""
    pnl_source: str = ""
    pnl_observed_at: float = 0.0
    pnl_reason_code: str = ""

    def get(self, key: str, default: Any = None) -> Any:
        """Dict-like accessor for canonical fields.

        Only the broker/API volume field is supported for size. Legacy volume aliases are intentionally not mapped here.
        """
        if key == "type":
            return "buy" if self.direction == 1 else "sell" if self.direction == -1 else default
        if key == "tradeSide":
            return "BUY" if self.direction == 1 else "SELL" if self.direction == -1 else default
        alias = {
            "ticket": "position_id",
            "positionId": "position_id",
            "symbolName": "symbol",
            "open_price": "entry_price",
            "price_open": "entry_price",
            "price_current": "current_price",
            "current_price": "current_price",
            "profit": "pnl",
        }.get(key, key)
        return getattr(self, alias, default)


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


ReconcileStatus = Literal["fresh", "cache", "event", "failed"]
_RECONCILE_STATUSES = frozenset({"fresh", "cache", "event", "failed"})
ReconcileComponentState = Literal["known", "unknown", "stale", "error"]
_RECONCILE_COMPONENT_STATES = frozenset({"known", "unknown", "stale", "error"})


@dataclass(frozen=True)
class ReconcileComponentFact:
    """Immutable truth for one component of a broker reconciliation.

    Position identity/volume/SL/TP and price/PnL do not share a freshness
    boundary in cTrader: ``ProtoOAReconcileRes`` is authoritative for the
    former, while price comes from spot events and PnL from a separate RPC.
    Keeping these facts separate prevents a successful identity snapshot from
    manufacturing a zero PnL or entry-price mark.
    """

    state: ReconcileComponentState
    source: str = ""
    observed_at: float = 0.0
    reason_code: str = ""
    known_position_ids: tuple[int, ...] = ()
    unknown_position_ids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.state not in _RECONCILE_COMPONENT_STATES:
            raise ValueError(f"invalid reconcile component state: {self.state!r}")

    @property
    def known(self) -> bool:
        return self.state == "known"


@dataclass(frozen=True)
class PositionReconcileResult:
    """Immutable, explicit result of a broker position reconciliation.

    ``fresh`` is the only authoritative full broker snapshot.  ``cache`` and
    ``event`` deliberately remain distinguishable so safety callers cannot
    mistake a stale/cache snapshot (including an empty one) for a successful
    empty-account reconciliation.  ``failed`` never carries an authoritative
    answer.
    """

    reconcile_id: str
    status: ReconcileStatus
    positions: tuple[PositionInfo, ...] = ()
    observed_at: float = 0.0
    generated_at: float = 0.0
    error_code: str = ""
    error_message: str = ""
    identity_component: ReconcileComponentFact | None = None
    protection_component: ReconcileComponentFact | None = None
    price_component: ReconcileComponentFact | None = None
    pnl_component: ReconcileComponentFact | None = None

    def __post_init__(self) -> None:
        if self.status not in _RECONCILE_STATUSES:
            raise ValueError(f"invalid position reconcile status: {self.status!r}")
        position_ids = tuple(
            sorted(
                int(position.position_id or 0)
                for position in self.positions
                if int(position.position_id or 0) > 0
            )
        )
        if self.status == "fresh":
            snapshot_state: ReconcileComponentState = "known"
            snapshot_source = "broker_reconcile"
            snapshot_reason = ""
        elif self.status in {"cache", "event"}:
            snapshot_state = "stale"
            snapshot_source = self.status
            snapshot_reason = self.error_code or "not_freshly_reconciled"
        else:
            snapshot_state = "error"
            snapshot_source = "broker_reconcile"
            snapshot_reason = self.error_code or "position_reconcile_failed"
        snapshot_fact = ReconcileComponentFact(
            state=snapshot_state,
            source=snapshot_source,
            observed_at=float(self.observed_at or 0.0),
            reason_code=snapshot_reason,
            known_position_ids=position_ids if snapshot_state == "known" else (),
            unknown_position_ids=position_ids if snapshot_state != "known" else (),
        )
        if self.identity_component is None:
            object.__setattr__(self, "identity_component", snapshot_fact)
        if self.protection_component is None:
            object.__setattr__(self, "protection_component", snapshot_fact)
        unspecified = ReconcileComponentFact(
            state="unknown" if self.status != "failed" else "error",
            source="legacy_unspecified",
            observed_at=0.0,
            reason_code=(
                "component_not_reported"
                if self.status != "failed"
                else self.error_code or "position_reconcile_failed"
            ),
            unknown_position_ids=position_ids,
        )
        if self.price_component is None:
            object.__setattr__(self, "price_component", unspecified)
        if self.pnl_component is None:
            object.__setattr__(self, "pnl_component", unspecified)

    @property
    def success(self) -> bool:
        return self.status != "failed"

    @property
    def fresh(self) -> bool:
        return self.status == "fresh"

    @property
    def authoritative(self) -> bool:
        """Whether identity/volume/SL/TP are a fresh full snapshot.

        Price and PnL authority must be checked through ``components``.
        """
        return self.status == "fresh"

    @property
    def components(self) -> Mapping[str, ReconcileComponentFact]:
        return MappingProxyType(
            {
                "identity": self.identity_component,
                "protection": self.protection_component,
                "price": self.price_component,
                "pnl": self.pnl_component,
            }
        )


@dataclass(frozen=True)
class AccountReconcileResult:
    """Immutable, explicit result of a broker account reconciliation."""

    reconcile_id: str
    status: ReconcileStatus
    account: AccountInfo | None = None
    observed_at: float = 0.0
    generated_at: float = 0.0
    error_code: str = ""
    error_message: str = ""

    def __post_init__(self) -> None:
        if self.status not in _RECONCILE_STATUSES:
            raise ValueError(f"invalid account reconcile status: {self.status!r}")

    @property
    def success(self) -> bool:
        return self.status != "failed"

    @property
    def fresh(self) -> bool:
        return self.status == "fresh"

    @property
    def authoritative(self) -> bool:
        return self.status == "fresh"


# ── 抽象接口 ─────────────────────────────────────────────

class BaseBrokerBridge(ABC):
    """统一经纪商接口

    当前实盘默认仅走 cTrader 实现；Paper 与历史 MT5 实现并存于同一抽象层，用于兼容/回归场景。
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
                   comment: str = "", *, decision_id: str = "",
                   trade_id: str = "", risk_verdict: dict | None = None) -> OrderResult:
        """市价买入"""
        ...

    @abstractmethod
    def market_sell(self, symbol: str, volume: float,
                    sl: float = 0.0, tp: float = 0.0,
                    comment: str = "", *, decision_id: str = "",
                    trade_id: str = "", risk_verdict: dict | None = None) -> OrderResult:
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

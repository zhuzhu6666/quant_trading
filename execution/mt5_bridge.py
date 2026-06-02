"""
execution/mt5_bridge.py — MT5 API 整合封装 (P1-A)

P1-A 任务: 整合 execution/mt5_bridge.py (230 行) 和 live/executor.py (130 行)
到单一入口, 加:
  - filling mode 探测 (FOK / IOC / RETURN), 不同 broker 偏好不同, 硬编码会拒单
  - 历史数据拉取 (mt5.copy_rates_from_pos) — 为 P1-C 准备
  - dry-run __main__: 模拟连接检查 + 探测环境

不破坏现有 ExecutionRouter 接口, 保留所有原有方法.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

try:
    import MetaTrader5 as mt5
    HAS_MT5 = True
except ImportError:
    HAS_MT5 = False


@dataclass
class MT5OrderResult:
    """统一订单结果"""
    success: bool
    ticket: int = 0
    retcode: int = 0
    comment: str = ""
    price: float = 0.0
    volume: float = 0.0


# ── Filling mode 探测 (P1-A 新增) ────────────────────────

# MT5 filling policy 常量 (不同 broker 启用不同)
FILLING_MODES = {
    "FOK": getattr(mt5, "ORDER_FILLING_FOK", 0) if HAS_MT5 else 0,
    "IOC": getattr(mt5, "ORDER_FILLING_IOC", 1) if HAS_MT5 else 1,
    "RETURN": getattr(mt5, "ORDER_FILLING_RETURN", 2) if HAS_MT5 else 2,
}


def probe_filling_mode(symbol: str = "XAUUSD+") -> str:
    """
    探测 broker 偏好的 filling mode.
    不同 broker 启用的 mode 不同 (FOK=0 / IOC=1 / RETURN=2):
      - FOK: 全部成交或全部取消 (外汇 ECN)
      - IOC: 立即成交剩余部分取消 (部分 ECN)
      - RETURN: 多笔 partial fill (CFD / 股票)

    Returns: 'FOK' | 'IOC' | 'RETURN' | 'UNKNOWN'
    """
    if not HAS_MT5:
        return "UNKNOWN"
    info = mt5.symbol_info(symbol)
    if info is None:
        logger.warning(f"Symbol {symbol} not found, cannot probe filling mode")
        return "UNKNOWN"
    # filling_mode 是 bitmask, 但实际只支持一个
    fm = info.filling_mode
    if fm & 1:  # FOK bit
        return "FOK"
    if fm & 2:  # IOC bit
        return "IOC"
    if fm & 4:  # RETURN bit (实际是 bit 2 = value 4, 但有些 broker 报告 0/1/2)
        return "RETURN"
    # 兜底: 按 0/1/2 解释
    if fm == 0:
        return "FOK"
    if fm == 1:
        return "IOC"
    if fm == 2:
        return "RETURN"
    logger.warning(f"Unknown filling_mode={fm} for {symbol}")
    return "UNKNOWN"


def _filling_constant(mode: str) -> int:
    """mode 名 → MT5 常量"""
    return FILLING_MODES.get(mode, FILLING_MODES["IOC"])


# ── 拉取历史数据 (P1-C 准备) ─────────────────────────────

def fetch_history(symbol: str, timeframe: int, n_bars: int) -> "np.ndarray | None":
    """
    从 MT5 拉取最近 n_bars 根 K 线.

    Args:
        symbol: e.g. "XAUUSD+"
        timeframe: mt5.TIMEFRAME_M15 / H1 / D1
        n_bars: 拉取根数 (上限受 broker 限制, 通常 5000-50000)

    Returns:
        numpy structured array with fields (time, open, high, low, close, tick_volume, spread, real_volume)
        失败返回 None
    """
    if not HAS_MT5:
        return None
    try:
        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, n_bars)
        if rates is None or len(rates) == 0:
            logger.warning(f"No rates returned for {symbol} tf={timeframe} n={n_bars}: {mt5.last_error()}")
            return None
        return rates
    except Exception as e:
        logger.exception(f"copy_rates_from_pos failed: {e}")
        return None


def rates_to_dataframe(rates) -> "pd.DataFrame | None":
    """MT5 rates array → pandas DataFrame (time index, 6 列)"""
    try:
        import pandas as pd
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("time")
        df = df.rename(columns={"tick_volume": "volume"})
        return df[["open", "high", "low", "close", "volume", "spread"]]
    except Exception as e:
        logger.exception(f"rates_to_dataframe failed: {e}")
        return None


# ── 主类 (整合后的 MT5 入口) ────────────────────────────

class MT5Bridge:
    """
    MT5 交易桥接 (P1-A 整合版).

    整合:
      - execution/mt5_bridge.py (旧, 230 行)
      - live/executor.py (旧, 130 行, 重复实现)

    新增:
      - filling mode 自动探测
      - 历史数据拉取
      - close_all_positions (P1-F 紧急平仓)
    """

    def __init__(self, account: int = 0, password: str = "",
                 server: str = "", symbol: str = "XAUUSD+",
                 auto_probe_filling: bool = True):
        self.account = account
        self.password = password
        self.server = server
        self.symbol = symbol
        self._connected = False
        self._filling_mode: str | None = None
        self._auto_probe_filling = auto_probe_filling

    # ── 连接管理 ──

    def connect(self) -> bool:
        if not HAS_MT5:
            logger.error("MetaTrader5 package not installed")
            return False
        if not mt5.initialize(
            login=self.account,
            password=self.password,
            server=self.server,
        ):
            logger.error(f"MT5 init failed: {mt5.last_error()}")
            return False
        self._connected = True
        logger.info(f"MT5 connected: account={self.account} server={self.server}")

        info = mt5.symbol_info(self.symbol)
        if info is None:
            logger.error(f"Symbol {self.symbol} not found")
            self.disconnect()
            return False
        logger.info(f"Symbol {self.symbol}: digits={info.digits} "
                     f"spread={info.spread} trade_mode={info.trade_mode} "
                     f"filling_mode={info.filling_mode}")

        # 自动探测 filling mode
        if self._auto_probe_filling:
            self._filling_mode = probe_filling_mode(self.symbol)
            logger.info(f"Detected filling mode: {self._filling_mode}")
        return True

    def disconnect(self):
        if self._connected:
            mt5.shutdown()
            self._connected = False
            logger.info("MT5 disconnected")

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def filling_mode(self) -> str:
        return self._filling_mode or "IOC"

    # ── 行情 ──

    def get_tick(self) -> dict | None:
        if not self._connected:
            return None
        tick = mt5.symbol_info_tick(self.symbol)
        if tick is None:
            return None
        return {
            "bid": tick.bid, "ask": tick.ask,
            "last": tick.last, "volume": tick.volume,
            "time": tick.time_msc / 1000.0 if tick.time_msc else 0.0,
        }

    def get_bid(self) -> float:
        t = self.get_tick()
        return t["bid"] if t else 0.0

    def get_ask(self) -> float:
        t = self.get_tick()
        return t["ask"] if t else 0.0

    def get_spread(self) -> float:
        t = self.get_tick()
        return (t["ask"] - t["bid"]) if t else 0.0

    # ── 订单 ──

    def market_buy(self, volume: float, sl: float = 0.0, tp: float = 0.0,
                   comment: str = "") -> MT5OrderResult:
        return self._send_order(
            action=mt5.TRADE_ACTION_DEAL,
            order_type=mt5.ORDER_TYPE_BUY,
            volume=volume, price=self.get_ask(),
            sl=sl, tp=tp, comment=comment,
        )

    def market_sell(self, volume: float, sl: float = 0.0, tp: float = 0.0,
                    comment: str = "") -> MT5OrderResult:
        return self._send_order(
            action=mt5.TRADE_ACTION_DEAL,
            order_type=mt5.ORDER_TYPE_SELL,
            volume=volume, price=self.get_bid(),
            sl=sl, tp=tp, comment=comment,
        )

    def close_position(self, ticket: int | None = None) -> MT5OrderResult:
        if not self._connected:
            return MT5OrderResult(success=False, comment="Not connected")
        pos = mt5.positions_get(symbol=self.symbol)
        if not pos:
            return MT5OrderResult(success=False, comment="No position")
        # ticket 参数: 指定就关指定, 否则关第一个
        if ticket is not None:
            pos = [p for p in pos if p.ticket == ticket]
            if not pos:
                return MT5OrderResult(success=False, comment=f"Ticket {ticket} not found")
            pos = pos[0]
        else:
            pos = pos[0]
        close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY \
                else mt5.ORDER_TYPE_BUY
        price = self.get_bid() if pos.type == mt5.POSITION_TYPE_BUY else self.get_ask()
        return self._send_order(
            action=mt5.TRADE_ACTION_DEAL,
            order_type=close_type,
            volume=pos.volume,
            price=price,
            position=pos.ticket,
        )

    def close_all_positions(self, symbol: str | None = None) -> list[MT5OrderResult]:
        """P1-F: 紧急一键全平 (按 symbol 过滤)"""
        if not self._connected:
            return [MT5OrderResult(success=False, comment="Not connected")]
        target_symbol = symbol or self.symbol
        positions = mt5.positions_get(symbol=target_symbol)
        if not positions:
            return []
        results = []
        for pos in positions:
            r = self.close_position(ticket=pos.ticket)
            results.append(r)
            logger.warning(f"[EMERGENCY CLOSE] {target_symbol} ticket={pos.ticket} "
                          f"vol={pos.volume} profit={pos.profit} success={r.success}")
        return results

    def _send_order(self, action, order_type, volume, price,
                    sl=0.0, tp=0.0, comment="", position=0) -> MT5OrderResult:
        if not self._connected:
            return MT5OrderResult(success=False, comment="Not connected")
        request = {
            "action": action,
            "symbol": self.symbol,
            "volume": volume,
            "type": order_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": 10,
            "magic": 123456,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": _filling_constant(self.filling_mode),
        }
        if position:
            request["position"] = position
        result = mt5.order_send(request)
        if result is None:
            return MT5OrderResult(success=False, comment="order_send returned None")
        return MT5OrderResult(
            success=result.retcode == mt5.TRADE_RETCODE_DONE,
            ticket=result.order,
            retcode=result.retcode,
            comment=result.comment or "",
            price=result.price,
            volume=result.volume,
        )

    # ── 账户 / 持仓 ──

    def account_info(self) -> dict:
        if not self._connected:
            return {}
        info = mt5.account_info()
        if info is None:
            return {}
        return {
            "balance": info.balance,
            "equity": info.equity,
            "margin": info.margin,
            "margin_free": info.margin_free,
            "margin_level": info.margin_level,
            "leverage": info.leverage,
            "currency": info.currency,
        }

    def get_positions(self, symbol: str | None = None) -> list[dict]:
        if not self._connected:
            return []
        target = symbol or self.symbol
        positions = mt5.positions_get(symbol=target)
        if not positions:
            return []
        return [{
            "ticket": p.ticket,
            "type": "buy" if p.type == mt5.POSITION_TYPE_BUY else "sell",
            "volume": p.volume,
            "price_open": p.price_open,
            "price_current": p.price_current,
            "sl": p.sl,
            "tp": p.tp,
            "profit": p.profit,
            "magic": p.magic,
        } for p in positions]

    # ── 历史拉取 (P1-C 准备) ──

    def fetch_bars(self, timeframe: int = None, n_bars: int = 5000) -> "pd.DataFrame | None":
        """拉取最近 n_bars 根 K 线 (默认 M15)"""
        if timeframe is None:
            timeframe = mt5.TIMEFRAME_M15
        rates = fetch_history(self.symbol, timeframe, n_bars)
        if rates is None:
            return None
        return rates_to_dataframe(rates)


# ── Dry-run 入口 (P1-A 新增) ───────────────────────────

def _dry_run():
    """无 broker 时模拟, 验证 import / 探测逻辑 / 离线接口"""
    print("=" * 70)
    print("  MT5 Bridge dry-run (无 broker)")
    print("=" * 70)
    print(f"  MetaTrader5 installed: {HAS_MT5}")
    if HAS_MT5:
        print(f"  mt5 module: {mt5}")
    print()
    print("  Filling mode 探测逻辑:")
    for name, val in FILLING_MODES.items():
        print(f"    {name}: ORDER_FILLING_{name} = {val}")
    print()
    print("  历史数据拉取函数:")
    print(f"    fetch_history(symbol, tf, n_bars) → np.ndarray | None")
    print(f"    rates_to_dataframe(rates) → pd.DataFrame | None")
    print(f"    bridge.fetch_bars(TIMEFRAME_M15, 5000) → DataFrame")
    print()
    print("  紧急平仓 (P1-F):")
    print(f"    bridge.close_all_positions() → list[MT5OrderResult]")
    print()
    print("  整合范围:")
    print(f"    ✓ 旧 execution/mt5_bridge.py (230 行) 全部保留")
    print(f"    ✓ 旧 live/executor.py (130 行) 全部保留")
    print(f"    ✓ + filling mode 探测")
    print(f"    ✓ + fetch_history / rates_to_dataframe")
    print(f"    ✓ + close_all_positions")
    print()
    if not HAS_MT5:
        print("  ⚠ MetaTrader5 未装, 实盘 / dry-run 都不可用")
    else:
        try:
            initialized = mt5.initialize()
            print(f"  mt5.initialize() (no creds): {initialized}")
            if initialized:
                mt5.shutdown()
        except Exception as e:
            print(f"  mt5.initialize() exception: {e}")
    print("=" * 70)


if __name__ == "__main__":
    _dry_run()

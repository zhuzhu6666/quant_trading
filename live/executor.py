"""
实盘执行模块
连接MT5，接收策略信号，自动下单/平仓
"""
import time
import MetaTrader5 as mt5
from datetime import datetime
from loguru import logger
from config import MT5_LOGIN, MT5_PASSWORD, MT5_SERVER, SYMBOL


class Executor:
    """MT5实盘执行器"""

    def __init__(self, symbol: str = SYMBOL):
        self.symbol    = symbol
        self.connected = False

    def connect(self) -> bool:
        if mt5.initialize(login=MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER):
            self.connected = True
            info = mt5.account_info()
            logger.info(f"实盘连接成功  账户:{MT5_LOGIN}  余额:${info.balance}")
            return True
        logger.error(f"实盘连接失败: {mt5.last_error()}")
        return False

    def disconnect(self):
        mt5.shutdown()
        self.connected = False
        logger.info("实盘已断开")

    # ── 订单操作 ───────────────────────────────────────
    def market_order(self, direction: int, lots: float, sl: float = 0, tp: float = 0) -> dict:
        """
        市价开仓
        direction: 1=做多(buy), -1=做空(sell)
        lots     : 手数
        sl/tp    : 止损/止盈价格(0=不设置)
        返回: dict(order_result)
        """
        if not self.connected:
            return {"error": "未连接MT5"}

        action = mt5.TRADE_ACTION_DEAL
        order_type = mt5.ORDER_TYPE_BUY if direction == 1 else mt5.ORDER_TYPE_SELL
        price = mt5.symbol_info_tick(self.symbol).ask if direction == 1 else mt5.symbol_info_tick(self.symbol).bid

        request = {
            "action":      action,
            "symbol":      self.symbol,
            "volume":      lots,
            "type":        order_type,
            "price":       price,
            "sl":          sl,
            "tp":          tp,
            "deviation":   5,
            "magic":       20241001,
            "comment":     "quant_bot",
            "type_time":   mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(f"开仓成功  {'多' if direction==1 else '空'}  {lots}手  @{price:.2f}  SL:{sl}  TP:{tp}")
        else:
            logger.error(f"开仓失败 [{result.retcode}]: {result.comment}")
        return self._parse_result(result)

    def close_position(self, ticket: int, lots: float = 0) -> dict:
        """平仓"""
        if not self.connected:
            return {"error": "未连接MT5"}

        pos = mt5.positions_get(ticket=ticket)
        if not pos:
            return {"error": f"找不到持仓 #{ticket}"}

        pos = pos[0]
        direction = -1 if pos.type == mt5.ORDER_TYPE_SELL else 1
        price = mt5.symbol_info_tick(self.symbol).bid if direction == 1 else mt5.symbol_info_tick(self.symbol).ask

        request = {
            "action":      mt5.TRADE_ACTION_DEAL,
            "symbol":      self.symbol,
            "volume":      lots if lots > 0 else pos.volume,
            "type":        mt5.ORDER_TYPE_SELL if direction == -1 else mt5.ORDER_TYPE_BUY,
            "position":    ticket,
            "price":       price,
            "deviation":   5,
            "magic":       20241001,
            "comment":     "quant_close",
            "type_time":   mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(f"平仓成功  #{ticket}  @{price:.2f}")
        else:
            logger.error(f"平仓失败 [{result.retcode}]: {result.comment}")
        return self._parse_result(result)

    # ── 持仓查询 ───────────────────────────────────────
    def get_positions(self) -> list:
        """获取当前所有持仓"""
        if not self.connected:
            return []
        return list(mt5.positions_get(symbol=self.symbol))

    def get_orders(self) -> list:
        """获取当前挂单"""
        if not self.connected:
            return []
        return list(mt5.orders_get(symbol=self.symbol))

    # ── 结果解析 ───────────────────────────────────────
    @staticmethod
    def _parse_result(result) -> dict:
        if result is None:
            return {"ok": False, "error": "结果为空"}
        return {
            "ok":       result.retcode == mt5.TRADE_RETCODE_DONE,
            "ticket":   result.order,
            "retcode":  result.retcode,
            "comment":  result.comment,
            "deal":     result.deal,
            "volume":   result.volume,
        }

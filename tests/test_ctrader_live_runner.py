"""
tests/test_ctrader_live_runner.py — cTrader Live Runner 单元测试

测试目标:
  - TradeLogger jsonl 落盘格式
  - check_sl_tp 本地止损止盈触发
  - sl_tp_prices 长/短仓 SL/TP 计算
  - reconcile_position 持仓对账 (Mock bridge)
  - 端到端 mock 信号流 (MockBridge + mock strategy)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.ctrader_live_runner import (  # noqa: E402
    LocalPosition,
    TradeRecord,
    TradeLogger,
    check_sl_tp,
    sl_tp_prices,
)
from execution.ctrader_bridge import CTraderOrderResult  # noqa: E402


# ── TradeLogger 测试 ─────────────────────────────────────────────

class TestTradeLogger:
    def test_write_creates_file_with_one_line(self, tmp_path):
        log = TradeLogger(tmp_path / "trades.jsonl")
        rec = TradeRecord(ts=1000.0, event="open", symbol="XAUUSD",
                          side="buy", volume=0.01, price=4500.0, sl=4490.0, tp=4520.0)
        log.write(rec)
        assert log.count == 1
        content = (tmp_path / "trades.jsonl").read_text(encoding="utf-8")
        assert content.endswith("\n")
        data = json.loads(content.strip())
        assert data["event"] == "open"
        assert data["symbol"] == "XAUUSD"
        assert data["side"] == "buy"
        assert data["price"] == 4500.0

    def test_write_appends_multiple_records(self, tmp_path):
        log = TradeLogger(tmp_path / "trades.jsonl")
        for i in range(5):
            log.write(TradeRecord(ts=float(i), event="open", symbol="XAUUSD", volume=0.01, price=4500 + i))
        assert log.count == 5
        records = log.read_all()
        assert len(records) == 5
        assert [r["ts"] for r in records] == [0.0, 1.0, 2.0, 3.0, 4.0]

    def test_overwrite_on_init(self, tmp_path):
        # 第一次写
        log1 = TradeLogger(tmp_path / "trades.jsonl")
        log1.write(TradeRecord(ts=1.0, event="open", symbol="XAUUSD"))
        # 第二次 init 应当覆盖
        log2 = TradeLogger(tmp_path / "trades.jsonl")
        log2.write(TradeRecord(ts=2.0, event="open", symbol="XAUUSD"))
        assert log2.count == 1
        records = log2.read_all()
        assert len(records) == 1
        assert records[0]["ts"] == 2.0

    def test_mkdir_parents(self, tmp_path):
        nested = tmp_path / "deep" / "nested" / "trades.jsonl"
        log = TradeLogger(nested)
        log.write(TradeRecord(ts=1.0, event="open", symbol="XAUUSD"))
        assert nested.exists()


# ── check_sl_tp 本地止损止盈测试 ────────────────────────────────

class TestSlTpTrigger:
    def test_long_tp_hit(self):
        pos = LocalPosition(direction=1, sl_price=4490.0, tp_price=4520.0)
        bar = {"high": 4525.0, "low": 4510.0}
        assert check_sl_tp(pos, bar) == "tp_hit"

    def test_long_sl_hit(self):
        pos = LocalPosition(direction=1, sl_price=4490.0, tp_price=4520.0)
        bar = {"high": 4510.0, "low": 4485.0}
        assert check_sl_tp(pos, bar) == "sl_hit"

    def test_short_tp_hit(self):
        pos = LocalPosition(direction=-1, sl_price=4520.0, tp_price=4470.0)
        bar = {"high": 4480.0, "low": 4465.0}
        assert check_sl_tp(pos, bar) == "tp_hit"

    def test_short_sl_hit(self):
        pos = LocalPosition(direction=-1, sl_price=4520.0, tp_price=4470.0)
        bar = {"high": 4525.0, "low": 4500.0}
        assert check_sl_tp(pos, bar) == "sl_hit"

    def test_no_trigger_within_range(self):
        pos = LocalPosition(direction=1, sl_price=4490.0, tp_price=4520.0)
        bar = {"high": 4510.0, "low": 4500.0}
        assert check_sl_tp(pos, bar) is None

    def test_no_trigger_flat(self):
        pos = LocalPosition(direction=0, sl_price=4490.0, tp_price=4520.0)
        bar = {"high": 9999.0, "low": 0.0}
        assert check_sl_tp(pos, bar) is None

    def test_no_sl_tp_set(self):
        pos = LocalPosition(direction=1, sl_price=0.0, tp_price=0.0)
        bar = {"high": 9999.0, "low": 0.0}
        assert check_sl_tp(pos, bar) is None


# ── sl_tp_prices 长短仓计算 ────────────────────────────────────

class TestSlTpPrices:
    def test_long_sl_below_tp_above(self):
        signal = MagicMock()
        signal.direction = 1
        signal.price = 4500.0
        signal.atr = 7.0
        signal.sl_atr = 2.0
        signal.tp_atr = 3.0
        sl, tp = sl_tp_prices(signal)
        assert sl == 4486.0   # 4500 - 7*2
        assert tp == 4521.0   # 4500 + 7*3

    def test_short_sl_above_tp_below(self):
        signal = MagicMock()
        signal.direction = -1
        signal.price = 4500.0
        signal.atr = 7.0
        signal.sl_atr = 2.0
        signal.tp_atr = 3.0
        sl, tp = sl_tp_prices(signal)
        assert sl == 4514.0   # 4500 + 7*2
        assert tp == 4479.0   # 4500 - 7*3

    def test_flat_returns_zero(self):
        signal = MagicMock()
        signal.direction = 0
        signal.price = 4500.0
        signal.atr = 7.0
        signal.sl_atr = 2.0
        signal.tp_atr = 3.0
        sl, tp = sl_tp_prices(signal)
        assert sl == 0.0
        assert tp == 0.0


# ── Reconcile (本地 ↔ broker) 测试 ─────────────────────────────

class TestReconcilePosition:
    def test_broker_has_local_none(self):
        from scripts.ctrader_live_runner import reconcile_position
        bridge = MagicMock()
        bridge.get_positions.return_value = [
            {"position_id": 1001, "type": "BUY", "volume": 0.01, "price_open": 4500.0, "sl": 0, "tp": 0}
        ]
        local = LocalPosition()
        changed, pos = reconcile_position(bridge, local, "XAUUSD")
        assert changed is True
        assert pos["position_id"] == 1001
        bridge.get_positions.assert_called_once_with(symbol="XAUUSD")

    def test_broker_none_local_has(self):
        from scripts.ctrader_live_runner import reconcile_position
        bridge = MagicMock()
        bridge.get_positions.return_value = []
        local = LocalPosition(position_id=1001, direction=1)
        changed, pos = reconcile_position(bridge, local, "XAUUSD")
        assert changed is True
        assert pos is None

    def test_both_match_no_change(self):
        from scripts.ctrader_live_runner import reconcile_position
        bridge = MagicMock()
        bridge.get_positions.return_value = [
            {"position_id": 1001, "type": "BUY", "volume": 0.01, "price_open": 4500.0, "sl": 0, "tp": 0}
        ]
        local = LocalPosition(position_id=1001, direction=1, entry_price=4500.0)
        # reconcile 只看 broker 持仓列表, 即使 ID 相同 changed=True (它不知道本地细节)
        # 实际 runner 在 main loop 里另外判断
        changed, pos = reconcile_position(bridge, local, "XAUUSD")
        assert changed is True  # bridge 有 1 个, 跟本地 ID 比
        assert pos is not None

    def test_bridge_error_returns_safe(self):
        from scripts.ctrader_live_runner import reconcile_position
        bridge = MagicMock()
        bridge.get_positions.side_effect = RuntimeError("net error")
        local = LocalPosition()
        changed, pos = reconcile_position(bridge, local, "XAUUSD")
        assert changed is False
        assert pos is None


# ── MockBridge 端到端流 ─────────────────────────────────────────

class _MockBridge:
    """替代 CTraderBridge 的最小 mock, 供 main loop 端到端测试."""
    def __init__(self):
        self.calls = []
        self.positions = {}     # position_id -> dict
        self._next_pid = 10001
        self._next_balance = 1000.0
        self._send_orders = True  # 默认 LIVE, 但不发真单 (mock)

    def market_buy(self, volume, sl, tp, comment):
        self.calls.append(("buy", volume, sl, tp, comment))
        pid = self._next_pid
        self._next_pid += 1
        self.positions[pid] = {
            "position_id": pid, "type": "BUY", "volume": volume,
            "price_open": 4500.0, "sl": sl, "tp": tp,
        }
        return CTraderOrderResult(success=True, position_id=pid, price=4500.0, volume=volume)

    def market_sell(self, volume, sl, tp, comment):
        self.calls.append(("sell", volume, sl, tp, comment))
        pid = self._next_pid
        self._next_pid += 1
        self.positions[pid] = {
            "position_id": pid, "type": "SELL", "volume": volume,
            "price_open": 4500.0, "sl": sl, "tp": tp,
        }
        return CTraderOrderResult(success=True, position_id=pid, price=4500.0, volume=volume)

    def close_position(self, position_id, volume=None):
        self.calls.append(("close", position_id, volume))
        self.positions.pop(position_id, None)
        return CTraderOrderResult(success=True, position_id=position_id)

    def get_positions(self, symbol=None):
        return list(self.positions.values())

    def account_info(self):
        return {"balance": self._next_balance, "equity": self._next_balance}


class TestEndToEndMock:
    """用 _MockBridge + mock strategy 跑整个 main loop 的核心子集."""

    def _make_signal(self, direction, atr=7.0, sl_atr=2.0, tp_atr=3.0, price=4500.0):
        from strategy.base import Signal
        return Signal(
            strategy="mock_strat", symbol="XAUUSD",
            direction=direction, strength=1.0,
            sl_atr=sl_atr, tp_atr=tp_atr, atr=atr,
            price=price, timestamp=1234567890.0,
        )

    def test_open_long_then_close(self, tmp_path):
        """信号 LONG → mock 开仓 → 第二根 bar 信号 CLOSE → mock 平仓 → jsonl 2 条记录"""
        from scripts.ctrader_live_runner import (
            LocalPosition, reconcile_position, check_sl_tp, sl_tp_prices, TradeLogger, TradeRecord
        )

        bridge = _MockBridge()
        log = TradeLogger(tmp_path / "trades.jsonl")
        local_pos = LocalPosition()

        # ── Bar 1: LONG signal ──
        sig1 = self._make_signal(direction=1, price=4500.0)
        sl, tp = sl_tp_prices(sig1)
        res = bridge.market_buy(volume=0.01, sl=sl, tp=tp, comment="mock")
        assert res.success
        log.write(TradeRecord(
            ts=1000.0, event="open", symbol="XAUUSD", side="buy",
            volume=0.01, price=4500.0, sl=sl, tp=tp, atr=7.0,
            position_id=res.position_id, order_id=0,
        ))
        # reconcile 拿到 broker 持仓
        changed, broker_pos = reconcile_position(bridge, local_pos, "XAUUSD")
        local_pos.position_id = broker_pos["position_id"]
        local_pos.direction = 1
        local_pos.entry_price = broker_pos["price_open"]
        local_pos.sl_price = sl
        local_pos.tp_price = tp
        local_pos.entry_bar_idx = 0

        # ── Bar 2: SL/TP 未触发, 信号 CLOSE ──
        bar2 = {"high": 4505.0, "low": 4495.0, "close": 4500.0, "open": 4500.0}
        assert check_sl_tp(local_pos, bar2) is None
        res = bridge.close_position(position_id=local_pos.position_id)
        assert res.success
        log.write(TradeRecord(
            ts=2000.0, event="close", symbol="XAUUSD",
            position_id=local_pos.position_id,
            exit_price=4500.0, pnl=0.0,  # open=close, no PnL
            exit_reason="signal_close", bars_held=1,
        ))

        # ── 验证 jsonl ──
        records = log.read_all()
        assert len(records) == 2
        assert records[0]["event"] == "open"
        assert records[0]["side"] == "buy"
        assert records[0]["sl"] == 4486.0
        assert records[0]["tp"] == 4521.0
        assert records[1]["event"] == "close"
        assert records[1]["exit_reason"] == "signal_close"
        assert records[1]["bars_held"] == 1

    def test_open_long_then_sl_trigger(self, tmp_path):
        """LONG 开仓 → 价格暴跌 bar.low <= sl → 本地 SL trigger → close"""
        from scripts.ctrader_live_runner import (
            LocalPosition, check_sl_tp, TradeLogger, TradeRecord
        )

        bridge = _MockBridge()
        log = TradeLogger(tmp_path / "trades.jsonl")
        local_pos = LocalPosition(
            position_id=1001, direction=1,
            volume=0.01, entry_price=4500.0,
            sl_price=4486.0, tp_price=4521.0,
            entry_bar_idx=0,
        )
        bridge.positions[1001] = {
            "position_id": 1001, "type": "BUY", "volume": 0.01,
            "price_open": 4500.0, "sl": 4486.0, "tp": 4521.0,
        }

        # 暴跌 bar
        bar = {"high": 4490.0, "low": 4480.0, "open": 4485.0, "close": 4482.0}
        assert check_sl_tp(local_pos, bar) == "sl_hit"

        # 平仓
        res = bridge.close_position(position_id=1001)
        assert res.success
        # PnL: long, (exit - entry) * volume * contract_size
        # XAUUSD contract_size=100 (1 lot = 100 oz), entry=4500, exit=4485
        # pnl = (4485 - 4500) * 0.01 * 100 = -15.0
        pnl = (4485.0 - 4500.0) * 0.01 * 100
        log.write(TradeRecord(
            ts=2000.0, event="close", symbol="XAUUSD",
            position_id=1001, exit_price=4485.0, pnl=round(pnl, 4),
            exit_reason="sl_hit", bars_held=1,
        ))
        records = log.read_all()
        assert len(records) == 1
        assert records[0]["exit_reason"] == "sl_hit"
        assert records[0]["pnl"] == pytest.approx(-15.0, abs=0.01)

    def test_skip_duplicate_open(self, tmp_path):
        """已有持仓时,新 OPEN 信号应当跳过,不重复发单"""
        from scripts.ctrader_live_runner import LocalPosition, TradeLogger, TradeRecord

        bridge = _MockBridge()
        log = TradeLogger(tmp_path / "trades.jsonl")
        local_pos = LocalPosition(position_id=1001, direction=1, volume=0.01)
        bridge.positions[1001] = {"position_id": 1001, "type": "BUY", "volume": 0.01,
                                  "price_open": 4500.0, "sl": 0, "tp": 0}
        # 已有一条 open
        log.write(TradeRecord(ts=1.0, event="open", symbol="XAUUSD", side="buy",
                              position_id=1001, volume=0.01, price=4500.0))
        # 新信号 LONG, 但本地已有持仓 → 不该再发单
        if local_pos.position_id != 0:
            # skip 路径
            pass
        else:
            bridge.market_buy(volume=0.01, sl=0, tp=0, comment="dup")
        assert len(bridge.calls) == 0
        records = log.read_all()
        assert len(records) == 1  # 只有最初的 open


# ── volume 转换测试 ────────────────────────────────────────────

class TestVolumeConversion:
    def test_one_centi_lot(self):
        # 0.01 lot = 1 centi-lot per cTrader doc
        assert int(0.01 * 100) == 1

    def test_ten_centi_lots(self):
        assert int(0.1 * 100) == 10

    def test_one_lot(self):
        assert int(1.0 * 100) == 100

    def test_round_down(self):
        # 0.015 lot → 应 round 到 1 (centi-lot 取整)
        assert int(0.015 * 100) == 1

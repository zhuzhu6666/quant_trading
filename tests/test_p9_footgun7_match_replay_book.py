"""
tests/test_p9_footgun7_match_replay_book.py — Batch live_sync 修复

引自 framework_audit_20260604.md FOOTGUN-7:
execution/match_replay.py:100-101 book 只在首次 match_order 时建,
之后 self.bar 变化时, book 不更新, match_order 用旧 book 撮合。

修复: 每次 match_order 强制 rebuild book (cost < 1ms)。
"""
from execution.match_replay import MatchReplayEngine


def test_match_replay_rebuilds_book_each_call():
    """FOOTGUN-7: 两次 match_order 之间 bar 变了, 第二次应 rebuild book

    注: book.mid 来自 self._ticks[-1]['price'], 跟 self.bar 无关。
    要让 mid 变, 必须改 _ticks。
    """
    eng = MatchReplayEngine(bar={"open": 2000.0, "high": 2010.0, "low": 1990.0, "close": 2005.0})
    # 准备第一组 ticks (需要 price + volume 字段)
    eng._ticks = [{"price": 2000.0 + i * 0.1, "volume": 0.1} for i in range(100)]
    r1 = eng.match_order(side=1, size=0.01)
    book1_mid = eng._book["mid"]

    # 第二组 ticks 价格大幅变化
    eng._ticks = [{"price": 2050.0 + i * 0.1, "volume": 0.1} for i in range(100)]
    r2 = eng.match_order(side=1, size=0.01)
    book2_mid = eng._book["mid"]

    # 修复后: book2_mid 应当跟新 ticks 一致
    assert abs(book2_mid - 2059.9) < 0.01, (
        f"FOOTGUN-7 复发: book.mid={book2_mid}, 应跟随新 ticks=2059.9"
    )
    # 第一次的 mid 跟第二次应当不同
    assert abs(book1_mid - book2_mid) > 49.5, (
        f"FOOTGUN-7 复发: 两次 match_order book 一致 ({book1_mid}), 应不同"
    )

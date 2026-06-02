"""
Test: Signal dataclass 扩展字段 (Phase 7)

验证:
1. 三个新字段 (factor_scores, regime, confidence) 都能赋值
2. 默认值为 None (向后兼容)
3. 现有字段不受影响
4. Signal(**kwargs) 模式构造兼容 (即用现有调用方式仍能工作)
"""
import sys
from strategy.base import Signal


def test_default_values():
    """测试: 必填字段 + 默认 Optional 字段"""
    sig = Signal(strategy="test", symbol="XAUUSD", direction=1)
    print("\n[test_default_values] 必填 + 默认")
    print(f"  strategy      = {sig.strategy!r}")
    print(f"  symbol        = {sig.symbol!r}")
    print(f"  direction     = {sig.direction!r}")
    print(f"  strength      = {sig.strength!r}")
    print(f"  factor_scores = {sig.factor_scores!r}")
    print(f"  regime        = {sig.regime!r}")
    print(f"  confidence    = {sig.confidence!r}")

    assert sig.factor_scores is None, "factor_scores 默认应为 None"
    assert sig.regime is None, "regime 默认应为 None"
    assert sig.confidence is None, "confidence 默认应为 None"
    assert sig.direction == 1, "现有字段 direction 不应受影响"
    assert sig.strength == 1.0, "现有字段 strength 不应受影响"
    print("  [OK]")


def test_new_fields_assignment():
    """测试: 三个新字段都能赋值"""
    sig = Signal(
        strategy="MultiFactorM15",
        symbol="XAUUSD",
        direction=1,
        strength=0.85,
        sl_atr=1.5,
        tp_atr=3.0,
        atr=12.5,
        price=2350.0,
        timestamp=1717200000.0,
        meta={"votes_long": 4, "votes_short": 1},
        # ── 新字段 ──
        factor_scores={
            "rsi": 28.5,
            "adx": 31.2,
            "di_spread": 18.4,
            "macd_hist": 0.45,
            "stoch_k": 22.1,
        },
        regime={
            "TRENDING_UP": True,
            "RANGING": False,
            "HIGH_VOL": True,
            "LOW_VOL": False,
            "NEWS_DAY": False,
        },
        confidence=0.78,
    )
    print("\n[test_new_fields_assignment] 新字段赋值")
    print(f"  factor_scores = {sig.factor_scores}")
    print(f"  regime        = {sig.regime}")
    print(f"  confidence    = {sig.confidence}")

    assert sig.factor_scores == {"rsi": 28.5, "adx": 31.2, "di_spread": 18.4,
                                  "macd_hist": 0.45, "stoch_k": 22.1}
    assert sig.regime == {"TRENDING_UP": True, "RANGING": False,
                          "HIGH_VOL": True, "LOW_VOL": False, "NEWS_DAY": False}
    assert sig.confidence == 0.78
    # 验证元数据访问
    assert sig.factor_scores["adx"] == 31.2
    assert sig.regime["TRENDING_UP"] is True
    assert 0.0 <= sig.confidence <= 1.0
    print("  [OK]")


def test_partial_assignment():
    """测试: 三个字段互相独立, 可以只填一部分"""
    sig = Signal(
        strategy="GoldMomentum",
        symbol="XAUUSD",
        direction=-1,
        confidence=0.42,  # 只填一个
    )
    print("\n[test_partial_assignment] 只填 confidence")
    print(f"  factor_scores = {sig.factor_scores!r}")
    print(f"  regime        = {sig.regime!r}")
    print(f"  confidence    = {sig.confidence!r}")

    assert sig.factor_scores is None
    assert sig.regime is None
    assert sig.confidence == 0.42
    print("  [OK]")


def test_kwargs_pattern():
    """测试: Signal(**dict) 构造模式仍兼容 (虽然现在没人这么用)"""
    d = {
        "strategy": "MACDBB",
        "symbol": "XAUUSD",
        "direction": 1,
        "strength": 1.0,
        "factor_scores": {"macd_hist": 0.3},
        "regime": {"TRENDING_UP": True},
        "confidence": 0.65,
    }
    sig = Signal(**d)
    print("\n[test_kwargs_pattern] Signal(**dict)")
    print(f"  factor_scores = {sig.factor_scores}")
    print(f"  regime        = {sig.regime}")
    print(f"  confidence    = {sig.confidence}")

    assert sig.factor_scores == {"macd_hist": 0.3}
    assert sig.regime == {"TRENDING_UP": True}
    assert sig.confidence == 0.65
    print("  [OK]")


if __name__ == "__main__":
    print("=" * 60)
    print("Signal dataclass 扩展字段测试")
    print("=" * 60)
    test_default_values()
    test_new_fields_assignment()
    test_partial_assignment()
    test_kwargs_pattern()
    print("\n" + "=" * 60)
    print("ALL TESTS PASSED ✅")
    print("=" * 60)

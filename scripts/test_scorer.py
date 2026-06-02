"""
Test: WeightedScorer — 多策略信号加权打分验证

构造多空信号, 验证:
- 同方向加权分求和, 取总分高的方向
- fused_from 记录正确
- 动态调权生效
"""
import sys
import textwrap
from pathlib import Path

# ── 将项目根加入 sys.path ──────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from strategy.base import Signal
from strategy.scorer import WeightedScorer, DEFAULT_WEIGHTS


def make_signal(
    strategy: str,
    direction: int,
    confidence: float | None = None,
    strength: float = 1.0,
) -> Signal:
    """辅助构造 Signal (精简字段)"""
    return Signal(
        strategy=strategy,
        symbol="XAUUSD",
        direction=direction,
        strength=strength,
        confidence=confidence,
        meta={},
    )


def print_signals(title: str, signals: list[Signal], wscorer=None):
    """打印信号概览"""
    if wscorer is None:
        wscorer = scorer
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")
    print(f"  {'Strategy':<22s} {'Dir':<6s} {'Conf':<6s} {'Strength':<9s} {'Weight':<7s} {'Score':<7s}")
    print(f"  {'─' * 56}")
    for s in signals:
        w = wscorer.weights.get(s.strategy, 0.0)
        c = s.confidence if s.confidence is not None else s.strength
        score = w * c * abs(s.strength)
        dir_str = {1: "LONG", -1: "SHORT", 0: "FLAT"}.get(s.direction, str(s.direction))
        print(f"  {s.strategy:<22s} {dir_str:<6s} {c:<6.2f} {s.strength:<9.2f} {w:<7.2f} {score:<7.4f}")


def print_result(signal: Signal | None):
    """打印融合结果"""
    if signal is None:
        print("\n  ⛔ 融合结果: None (无有效信号)")
        return
    dir_str = {1: "LONG", -1: "SHORT", 0: "FLAT"}.get(signal.direction, str(signal.direction))
    fused = signal.meta.get("fused_from", []) if signal.meta else []
    ws = signal.meta.get("weighted_score", "N/A") if signal.meta else "N/A"
    ds = signal.meta.get("direction_total_score", "N/A") if signal.meta else "N/A"
    print(f"\n  ✅  Winner: {signal.strategy}")
    print(f"     方向: {dir_str}  |  weighted_score: {ws}  |  方向总分: {ds}")
    print(f"     融合来源 (fused_from): {fused}")


def check(cond: bool, msg: str):
    """简单断言"""
    status = "✓ PASS" if cond else "✗ FAIL"
    print(f"  [{status}] {msg}")
    if not cond:
        global _fail_count
        _fail_count += 1


_fail_count = 0


# ════════════════════════════════════════════════════════
#  1. 默认权重检查
# ════════════════════════════════════════════════════════
print("\n" + "█" * 60)
print("  阶段 1: 默认权重验证")
print("█" * 60)

scorer = WeightedScorer()
weights = scorer.get_weights()
print(f"  默认权重: {weights}")
check(abs(weights["multi_factor_m15"] - 0.4) < 1e-6, "multi_factor_m15 weight = 0.4")
check(abs(weights["ma_cross_h4"] - 0.2) < 1e-6, "ma_cross_h4 weight = 0.2")
check(abs(weights["macd_bb"] - 0.2) < 1e-6, "macd_bb weight = 0.2")
check(abs(weights["trend_following"] - 0.0) < 1e-6, "trend_following weight = 0.0")
check(abs(weights["breakout"] - 0.0) < 1e-6, "breakout weight = 0.0")

# ════════════════════════════════════════════════════════
#  2. 场景 A: 2 多头 vs 1 空头
# ════════════════════════════════════════════════════════
print("\n" + "█" * 60)
print("  阶段 2: 多头 vs 空头 — 多头应胜出")
print("█" * 60)

signals_a = [
    make_signal("multi_factor_m15", 1, confidence=0.8),
    make_signal("macd_bb", 1, confidence=0.6),
    make_signal("ma_cross_h4", -1, confidence=0.7),
]
print_signals("输入信号 (多头 2, 空头 1)", signals_a)

result_a = scorer.score(signals_a)
print_result(result_a)

check(result_a is not None, "融合结果不为 None")
check(result_a.direction == 1, "方向 = LONG (多头胜出)")

# 验证 fused_from
fused_a = result_a.meta.get("fused_from", [])
check("multi_factor_m15" in fused_a, "fused_from 包含 multi_factor_m15")
check("macd_bb" in fused_a, "fused_from 包含 macd_bb")
check("ma_cross_h4" not in fused_a, "fused_from 不含 ma_cross_h4 (空头方向)")

# 验证 weighted_score
ws_a = result_a.meta.get("weighted_score", 0)
check(abs(ws_a - 0.32) < 1e-4, f"最佳信号 weighted_score = 0.32 (实际 {ws_a})")

# 验证 fused_from 数量
check(len(fused_a) == 2, f"融合了 2 个策略 (实际 {len(fused_a)})")

# ════════════════════════════════════════════════════════
#  3. 场景 B: 再加一个多头 — 仍多头
# ════════════════════════════════════════════════════════
print("\n" + "█" * 60)
print("  阶段 3: 追加同方向信号 — 仍多头")
print("█" * 60)

signals_b = signals_a + [
    make_signal("multi_factor_m15", 1, confidence=0.5),
]
print_signals("输入信号 (多头 3, 空头 1)", signals_b)

result_b = scorer.score(signals_b)
print_result(result_b)

check(result_b is not None, "融合结果不为 None")
check(result_b.direction == 1, "方向 = LONG (仍多头胜出)")

# fused_from 应包含所有多头策略 (去重)
fused_b = result_b.meta.get("fused_from", [])
check("multi_factor_m15" in fused_b, "fused_from 包含 multi_factor_m15")
check("macd_bb" in fused_b, "fused_from 包含 macd_bb")
check(len(fused_b) == 2, f"融合了 2 个策略 (同名去重) (实际 {len(fused_b)})")

# 方向总分 = 0.4*0.8 + 0.4*0.5 + 0.2*0.6 = 0.32 + 0.20 + 0.12 = 0.64
expected_total = 0.4 * 0.8 + 0.4 * 0.5 + 0.2 * 0.6
ds_b = result_b.meta.get("direction_total_score", 0)
check(abs(ds_b - expected_total) < 1e-4, f"方向总分 = {expected_total} (实际 {ds_b})")

# ════════════════════════════════════════════════════════
#  4. 场景 C: 仅空头信号
# ════════════════════════════════════════════════════════
print("\n" + "█" * 60)
print("  阶段 4: 纯空头信号")
print("█" * 60)

signals_c = [
    make_signal("ma_cross_h4", -1, confidence=0.9),
    make_signal("macd_bb", -1, confidence=0.5),
]
print_signals("输入信号 (空头 2)", signals_c)

result_c = scorer.score(signals_c)
print_result(result_c)

check(result_c is not None, "融合结果不为 None")
check(result_c.direction == -1, "方向 = SHORT")
check("ma_cross_h4" in result_c.meta.get("fused_from", []), "fused_from 包含 ma_cross_h4")
check("macd_bb" in result_c.meta.get("fused_from", []), "fused_from 包含 macd_bb")

# ════════════════════════════════════════════════════════
#  5. 场景 D: 空信号 — 返回 None
# ════════════════════════════════════════════════════════
print("\n" + "█" * 60)
print("  阶段 5: 空列表 → 返回 None")
print("█" * 60)

result_d = scorer.score([])
print_result(result_d)
check(result_d is None, "空输入返回 None")

# ════════════════════════════════════════════════════════
#  6. 场景 E: 动态调权
# ════════════════════════════════════════════════════════
print("\n" + "█" * 60)
print("  阶段 6: 动态调权 — macd_bb 权重调至 0.5")
print("█" * 60)

scorer.update_weight("macd_bb", 0.5)
check(abs(scorer.weights["macd_bb"] - 0.5) < 1e-6, "macd_bb 权重已更新为 0.5")

signals_e = [
    make_signal("multi_factor_m15", 1, confidence=0.8),  # 0.4*0.8 = 0.32
    make_signal("macd_bb", 1, confidence=0.9),            # 0.5*0.9 = 0.45
]
print_signals("调权后信号", signals_e)
result_e = scorer.score(signals_e)
print_result(result_e)
check(result_e.strategy == "macd_bb", "调权后 macd_bb (0.45) 超过 multi_factor_m15 (0.32)")

# ════════════════════════════════════════════════════════
#  7. 场景 F: confidence 为 None → 回退到 strength
# ════════════════════════════════════════════════════════
print("\n" + "█" * 60)
print("  阶段 7: confidence=None → 回退 strength")
print("█" * 60)

scorer_f = WeightedScorer({"strat_a": 0.5, "strat_b": 0.5})
signals_f = [
    make_signal("strat_a", 1, confidence=None, strength=0.5),  # 回退到 strength: 0.5*0.5*0.5 = 0.125
    make_signal("strat_b", 1, confidence=0.9, strength=0.5),   # 0.5*0.9*0.5 = 0.225  ← 赢
]
print_signals("回退测试", signals_f, wscorer=scorer_f)
result_f = scorer_f.score(signals_f)
print_result(result_f)
check(result_f.strategy == "strat_b", "strat_b 的 confidence 更高 → 胜出")

# ════════════════════════════════════════════════════════
#  汇总
# ════════════════════════════════════════════════════════
print(f"\n{'=' * 60}")
if _fail_count == 0:
    print("  ✅ 全部测试通过!")
else:
    print(f"  ⚠  {_fail_count} 个检查失败")
print(f"{'=' * 60}\n")

sys.exit(0 if _fail_count == 0 else 1)

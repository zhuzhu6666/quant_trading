# 手数/合约规模/价值换算审计报告

## 🔴 CRITICAL: PnL 计算缺少 volume × contract_size 乘数

### Bug 1: live_service.py:2486 — 平仓回退 PnL

```python
total_pnl = (current_price - open_price) * dir_sign
```

完全没有 `volume × contract_size`。对于 0.05 lot 的持仓，$1 波动 = $5 PnL，但这里算出来只有 $1。

**影响**: 回退路径下的归因→session_trades 记录的 PnL 永远是 1 oz 的价格差，与实际持仓量无关。

### Bug 2: attribution_engine.py:341 — 归因引擎 PnL

```python
trade_pnl = (close_price - attrib.open_price) * attrib.direction
```

同样缺少 `volume × contract_size`。虽然 `TradeAttribution` 有 `api_volume` 字段（line 51），但 PnL 计算从未使用它。

**影响**: 所有因子归因数据中的 PnL 永远是 1 oz 的价格差，与实际仓位大小无关。

## 🟡 HIGH: Kelly 体积公式单位混乱 (live_service.py:104-154)

```python
contract_mult = 100.0        # line 146 — oz/lot
raw_api_volume = f_star * risk_capital / (sl_dist * contract_mult)  # line 147
vol_api = max(_min_vol, min(max_api_volume_calc * 100.0, raw_api_volume * 100.0, default_vol * 5))  # line 153
```

问题链:
1. `contract_mult = 100.0` 是 oz/标准手
2. `raw_api_volume` 结果单位 = 标准手数（contracts）
3. `* 100.0` 转换到 API units（100 API units = 1 standard lot）
4. 但当 Kelly 禁用时行 121-122: `return default_vol = _to_step(max(_min_vol, 100.0))`
5. 如果 `api_min_volume = 1`（0.01 lot），`default_vol = 100` = 1.00 标准手
6. 1 标准手 XAUUSD ≈ $433,000 名义本金 / $10K 账户 = **43 倍杠杆** — 危险！

**影响**: 非 Kelly 模式下默认开仓 1 标准手（100 oz），远超安全范围。

## 🟡 HIGH: 回测 vs 实盘手数单位不统一

| 模块 | 单位 | 0.01手含义 |
|------|------|-----------|
| 回测 (`vectorized.py`) | LOT_SIZE=0.01, CONTRACT_SIZE=100 | 1 oz |
| cTrader API | minVolume=1, lotSize=100 | 1 oz |
| `_risk_kelly_volume` (非Kelly) | default=100 API units | 100 oz (1 std lot) |

回测用 0.01 手（1 oz），但 Kelly 体积公式返回 100 API units（100 oz / 1 标准手）。

## 🟡 HIGH: runtime_config.py 合约乘数不一致

```python
config/runtime_config.py:272: "contract_size": 100,    # 正确
config/runtime_config.py:279: "contract_size": 100000,  # 其他品种?
```

100 vs 100000 差了 1000 倍。

## 修正方案

### Fix 1: AttributionEngine PnL (attribution_engine.py:341)

```python
trade_pnl = (close_price - attrib.open_price) * attrib.direction * attrib.api_volume
```

`api_volume` 已存储在 `TradeAttribution`。

### Fix 2: Live fallback PnL (live_service.py:2486)

```python
cpid_vol = _pos_open_api_volume.get(int(cpid), 0.01)
total_pnl = (current_price - open_price) * dir_sign * cpid_vol * 100.0
```

其中 100.0 是 cTrader contract_size（oz/API单位），cpid_vol 是 API units。

### Fix 3: Kelly non-Kelly default (live_service.py:120-122)

Kelly 禁用时不应返回 1 标准手：
```python
default_vol = _to_step(max(_min_vol, 0.01 * 100.0))  # 0.01 lot = 1 API unit
```

改为最小 0.01 手（1 API unit，而非 100 API units）。

### Fix 4: runtime_config contract_size 值 (config/runtime_config.py:279)

确认该品种的真实 contract_size。

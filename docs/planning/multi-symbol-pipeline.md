# 多品种独立 Pipeline 设计文档

## 现状

当前系统硬编码 `XAUUSD+` M5 为唯一交易品种:
- `_factor_pipeline` 全局变量仅存储单品种管道
- `_process_tick_factor_pipeline()` 无 symbol 参数
- `_run_loop()` 每次 tick 只处理一个品种
- `_warmup_from_local_db()` 默认 XAUUSD+
- 所有策略/因子/归因绑定到 global 单例

## 目标

支持 N 个品种独立运行各自的因子管道:
- 各品种有自己的 `StreamingFactorEngine` / `Normalizer` / `Compositor` / `Gate`
- 各品种的因子计算、信号、归因完全隔离
- 公共资源 (cTrader bridge, RuntimeConfig, AWE) 共享
- 风控 (RiskGovernor) 跨品种聚合

## 架构

```
_run_loop():
  │ 每 tick
  ├─ for symbol in enabled_symbols:
  │   │  挂历 = symbols[symbol]  (M5 / M15 等)
  │   ├─ 读取该品种新 bar
  │   ├─ factor_pipelines[symbol].engine.append_bar(bar)
  │   ├─ factor_pipelines[symbol].normalizer.normalize(...)
  │   ├─ factor_pipelines[symbol].compositor.compose(...)
  │   ├─ factor_pipelines[symbol].gate.filter(...)
  │   ├─ per-symbol 仓位管理
  │   └─ per-symbol 归因 (attribution)
  │
  ├─ 跨品种风控 (RiskGovernor, CrossAssetCovariance)
  └─ 全局状态更新
```

## 实施步骤

### Step 1: Pipeline Init (最小改动)

在 `_run_loop()` 管道初始化处, 遍历 `enabled_symbols`:

```python
_factor_pipelines = {}
for symbol in symbols:
    engine = StreamingFactorEngine(max_buffer=200)
    normalizer = SignalNormalizer(cfg.factor_signal_config)
    compositor = PortfolioCompositor(...)
    gate = ExecutionGate(...)
    _factor_pipelines[symbol] = {
        "engine": engine, "normalizer": normalizer,
        "compositor": compositor, "gate": gate,
        "attribution": None,  # 后续共享或独立
    }
# 保留 _factor_pipeline 为默认品种 (向后兼容)
_factor_pipeline = _factor_pipelines.get("XAUUSD+")
```

### Step 2: Per-Symbol Warmup

`_warmup_from_local_db(symbol, timeframe, n_bars)` 已经接收 symbol 参数。

当前 `_run_loop()` 只 warmup XAUUSD+。改成:

```python
for symbol, pipe in _factor_pipelines.items():
    df = _warmup_from_local_db(symbol, TF, 200)
    if df is not None:
        for _, bar in df.iterrows():
            pipe["engine"].append_bar(bar.to_dict())
            pipe["normalizer"].warmup(...)
```

### Step 3: Per-Symbol Tick Processing

`_process_tick_factor_pipeline()` 需加 `symbol` 参数。
当前硬编码 `_tf = "M5"` 和品种名。改成从 pipeline 配置读。

调用处改为:

```python
for symbol, pipe in _factor_pipelines.items():
    _process_tick_factor_pipeline(bridge, pipe, df_new, last_bar, broker, tick, log, symbol=symbol)
```

### Step 4: Per-Symbol Position Management

`_prev_position_ids` 当前是单品种 set。改成 `dict[symbol, set]`。
`_pos_entry_scores` 同理改成 `dict[symbol, dict]`。

### Step 5: Cross-Symbol Aggregation

- AWE: 跨品种聚合归因
- RiskGovernor: 聚合所有品种的仓位/浮亏
- 系统健康检查: 各品种独立

## 风险

1. **cTrader 订阅**: 每个品种需单独 `subscribe_spots()` 和 `subscribe_depth()`
2. **归因引擎**: AttributionEngine 当前是单例, 需决定共享还是按品种独立
3. **前端展示**: WS 推送需按 symbol 分组
4. **EvolutionKernel**: 每个品种需独立 evolution cycle

## 优先级

- **高**: Step 1 (多品种 pipeline init, 已有 `_factor_pipelines` 骨架)
- **中**: Step 3 (per-symbol tick)
- **低**: Step 4-5 (跨品种聚合)

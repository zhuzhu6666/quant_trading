# TODO — 修复清单 & 剩余工作

## ✅ 已完成 (PROJECT_AUDIT_v10 修复)

### P0 全部修复 ✅
| ID | 文件 | 修复 |
|----|------|------|
| P0-1 | `execution/oms.py:113` | `volume if volume is not None else order.volume` (原 falsy guard 误杀 volume=0) |
| P0-2 | `execution/order_retry.py:78` | 最后一次 attempt 不模拟拒绝，直接调 order_fn |
| P0-3 | `execution/ctrader_bridge.py:224` | BaseBrokerBridge 统一接口，symbol 解析失败检查返回值 |
| P0-4 | `backend/api/live.py:97` | `/api/live/strategy-status` 加 `_user: RequireUser` |
| P0-5 | `backend/api/external_data.py:106` | `trigger_refresh()` 加 `_user: RequireUser` |
| P0-6 | `backend/api/external_data.py:22` | `_cleanup_stale_jobs()` — 1h TTL + 500 条上限 |
| P0-7 | `DataPanel.tsx:320` | `fetch` → `authFetch` (带 JWT) |
| P0-8 | `MainDashboard.tsx:165` | `stopLoop`/`emergencyClose` 添加 `if (!r.ok)` 检查 |
| P0-9 | `MainDashboard.tsx:195` | `innerValue={dd * 100}` 修正 drawdown_pct 合约漂移 |

### P1 部分修复 ✅
| ID | 文件 | 修复 |
|----|------|------|
| P1-1 | `backend/core/auth.py:15` | JWT_SECRET 从环境变量 `QUANT_JWT_SECRET` 读取 |
| P1-2 | `backend/core/auth.py:39-53` | `get_current_user()` 改为调 `require_user()`，任何错误抛 401 |

---

## 🔴 待修复 (按优先级)

### ⚡ 5 分钟
- [ ] **P1-5**: `backend/ws/endpoints.py` — WebSocket 添加 token query param 认证
- [ ] **P1-24**: `backend/services/live_service.py:126` — `_cache_get_or_refresh` 硬编码 key "ctrader" → MT5 缓存永远 miss

### 🔧 30 分钟
- [ ] **P1-18~23**: 替换所有 `datetime.utcfromtimestamp`/`datetime.utcnow` (11 处，4 文件)
- [ ] **P1-6**: `execution/ctrader_bridge.py:250` — spot price 除数 `10**5` 硬编码为 XAUUSD
- [ ] **P1-10**: `execution/router.py:154-177` — `on_fill` 对反向成交只 log 不处理，仓位漂移
- [ ] **P1-13**: `execution/market_impact.py:116` — 成本计算用 1.0 而非实际金价 ~$3000

### 🏗️ 大工程
- [ ] Pandas 废弃 API 清理 (`DataFrame.append` → `pd.concat`, ~10-20 处真实调用)
- [ ] 前端 AbortController 全面接入 (8+ 处 polling)
- [ ] CausalCheck 集成到 AWE (当前注释掉)
- [ ] 死导入清理 (50+ 处)
- [ ] `tests/alpha/` 添加 `__init__.py`

---

## 🧪 基础设施待建 (Phase 5/7 蓝图)
- [ ] 向量化回测引擎 (alpha/backtest/)
- [ ] ML 预测管道注册为因子 (XGBoost/LightGBM)
- [ ] 特征工程自动化 (PCA/KPCA/Autoencoder)
- [ ] VaR/CVaR 风控引擎
- [ ] 归因 Dashboard 数据管道
- [ ] 执行质量分析 (ExecutionAnalytics)
- [ ] 多品种扩展 (并行管道)

# Web Console Audit v7 — 2026-06-08 (Playwright e2e 验证)

## 背景

v5/v6 静态审计修完 6+1 个 P0,但**截图 (012054.png) 显示 /factors 仍然崩**。
v6 修了 toFixed() 守卫后,页面**不崩了但全显示 "--"** — 静态审计**没看真后端 schema**,误
以为 schema 是 `{name, abs_ic, stability, ...}` 扁平结构,实际是
`{factor, components: {mean_abs_ic, ic_stability, ...}}` 嵌套结构。

**教训**:v5/v6 都是读代码 + 文档推断,**没真 fetch 一次 API 验证**。装 Playwright
跑 e2e 后,**单次扫描 18 页面 1.1 分钟找出 3 个真 bug**(v5/v6 全静态 0 个找出
这类 schema 错配),这 3 个 bug 都是**写代码时凭印象没看实际数据**造成。

## e2e 投资回报

| 项目                         | v5/v6 静态     | v7 e2e (本次)  |
| ---------------------------- | -------------- | -------------- |
| 找出 schema 错配类 bug       | 0              | 2              |
| 找出"代码改对了但路径没改"   | 0              | 1              |
| 找出"端点压根没注册"         | 0              | 1              |
| 找出"加速用错 dtype"         | 0              | 1              |
| 跑测时间                     | 30 min 读代码  | 1.1 min e2e    |
| **修改/通过页数**            | 6 P0+1 guard   | **18 页面 + 4 functional 全过** |

## 找到的 4 个新 P0 (e2e-only)

### Bug X — market.py 时间戳全部 = 1 (K线画不出)

**症状**: `/market` 页 TradingView 报
> "Assertion failed: data must be asc ordered by time, index=1, time=1"

e2e 18 次 pageerror 全是同一个。

**根因**: v5-B-3 修 `df.iterrows() → vectorized` 时我加了
`times = df.index.astype("int64") // 1_000_000_000`,**假设** pandas 3.x 默认
`datetime64[ns]`。但 `data.store.DataStore.load_bars` 实际返的是
`datetime64[s]` (second 精度),`2026-05-15.astype(int64) // 1e9 = 1`,
**所有 bar 时间戳塌成 1**,TradingView 拒收。

**修法**:
- v7-fix-4: `times = df.index.astype("int64")` (直接拿 unix sec,不除)
- v7-fix-1: tail 切片后**重新**计算 times (避免 df/times 长度失配)

**验证**: `GET /api/market/bars?symbol=XAUUSD%2B&timeframe=H1&limit=20` 返
`[1780534800, 1780538400, 1780542000, ...]` (2026-06-04 起,asc 排序,unique)

### Bug Y — /api/backtest (no slash) 端点 404

**症状**: `/backtest` 页 `/api/backtest?status=done` 拿到 `<!DOCTYPE html>` 404,
`fetch().json()` 抛 "Unexpected token '<'"。

**根因**:
1. **前端** fetch 的是 `/api/backtest?status=done` (没 trailing slash)
2. **后端** FastAPI router 是 `prefix="/api/backtest"` + `@router.get("/")`,
   生成的是 `/api/backtest/` (有 slash),**没有** `/api/backtest` (无 slash)
3. Next.js rewrites pass-through,FastAPI 找不到,返 404 HTML,前端崩

**修法** (v7-fix-5): 后端加 `@router.get("")` 同 handler,映射到 `list_jobs`。

**验证**:
- `GET /api/backtest?status=done` → `{"jobs":[]}` ✓
- `GET /api/backtest/12345` → `{"detail":"job not found"}` ✓ (FastAPI 标准 404)

### Bug Z — calibrator 页面 18 个 toFixed 崩

**症状**: `/calibrator` 18 次 pageerror "Cannot read properties of undefined (reading 'toFixed')"。

**根因**: 前端假设 buckets 是 `{bin, raw, calibrated, n}` 对象数组,
后端实际返 `[[low, high, value], ...]` 3-tuple 数组。`b.raw` / `b.calibrated`
`b.bin` 全是 undefined。

**修法** (v7-fix-3): 前端适配两种形态,on-the-fly 拆包成 `{low, high, cal}`,
`n` 字段后端 schema 不存,显示 `--`。

**验证**: 表格 8 行,首行 `0.10–0.20 | 0.100 | 0.000 | --` ✓

### Bug W — factors 列表 schema 错配 (v6 没真看出)

**症状**: v6 修 toFixed() 守卫后,所有 metric 列显示 `--`,**因为字段全在
`components.*` 子对象下**,前端读的 `f.abs_ic` / `f.stability` 全是 undefined。

**根因**: v5 audit 我读 README/PROJECT_MAP 推断 schema,**没真 fetch 一次**。
后端实际 schema:
```json
{ "factor": "rsi_14", "score": 23.54, "status": "DECAYING",
  "components": { "mean_abs_ic": 36.5, "ic_stability": 0.0,
                  "regime_consistency": 15.27, "decay_rate": 6.23,
                  "independence": 52.63 },
  "n_obs": 5000, "rolling_ic": -0.0001 }
```

**修法** (v6-fix-2 + v7-fix-6): 加 `flat()` 适配器,`f.factor → name`,
`f.components.mean_abs_ic → abs_ic`,等等。`mean_abs_ic` 是 **0-100 的
5 维评分** (factor_health.py:124 docstring "0-100 score for each of
5 dims"),**不是** 原始 IC 值,雷达图 `max` 从 0.1 改 100。

**验证**: factors 首行 `rsi_14 | DECAYING | 23.5 | 36.5000 | 0.00 | 15.27` ✓

## e2e 基础架构 (留用)

新增 `frontend/e2e/`:
- `critical_paths.spec.ts` — 18 页面 mount + 验证无 pageerror / console error
- `functional_checks.spec.ts` — 4 页面 + 真 DOM 验证 (factors 数据/calibrator
  bucket 行数/market chart 出现/backtest 按钮可见)

**复用 playwright.config.ts** 既有 webServer 配置(backend :8000 + frontend :3000)。
**`reuseExistingServer: true`** — 用户已启的 dev server 直接用,不用启停。

**`PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1`** — 不装 chromium binary(网络受限),
用系统 Chrome 即可(`@playwright/test` 自带 channel="chrome" 支持,待改)。

## 跑法

```bash
# 一次性 (用户首次跑会装 playwright + chromium)
cd frontend && npm i

# 跑全部 (前提: backend :8000 + frontend :3000 在跑)
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npx playwright test --project=chromium
```

## v7 验证结果

| 测                          | 通过 | 失败 | 备注                |
| --------------------------- | ---- | ---- | ------------------- |
| critical_paths (18 页面)    | 18   | 0    | 1.0 min             |
| functional_checks (4 页面)  | 4    | 0    | 16 sec              |
| **合计**                    | **22** | **0** | **1.3 min 全过** |

## 仍留 future (不动)

- backtest_runner stub(B-2,按护栏模式留 TODO,真 backtrader optstrategy 1-2 周)
- paper symbol select / factors 轮询 / live start-stop dead code 等 P1
- playwright.config 改用 system Chrome (避免装 chromium)
- chromium 浏览器二进制走 `playwright install` (需要网络可达 playwright.azureedge.net)

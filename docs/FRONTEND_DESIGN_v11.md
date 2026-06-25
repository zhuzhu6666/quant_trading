# 前端设计文档 v11 — 符合 Phase 0-7 完成状态

> **归档说明（2026-06-25）**: 本文描述的是旧 `frontend-v2` / Web Console 扩展方案。当前唯一维护前端是 `miniprogram_v2`，新 UI 工作默认进入微信小程序。本文只保留作历史设计参考，不能作为当前实现入口。

> **日期**: 2026-06-15
> **版本**: 1.0
> **目标**: 基于 Factor Takeover v4 Phase 0-7 全部完成状态，设计新前端页面，补全缺失的 Risk / Ops / Backtest 视图

---

## 1. 现状分析

### 1.1 已有前端（5 面板 + 主仪表盘）

| 面板 | 文件 | 内容 | 状态 |
|------|------|------|------|
| 主仪表盘 | `MainDashboard.tsx` | KPI、权益曲线、调度任务、系统状态、因子权重、归因概览 | ✅ 已运行 |
| 交易 | `TradingPanel.tsx` | paper/live 双标签、模拟盘状态、实盘持仓、紧急平仓 | ✅ 已运行 |
| 因子 | `FactorsPanel.tsx` | 因子健康、发现、影子、ML 四标签 | ✅ 已运行 |
| 实验 | `ExperimentsPanel.tsx` | 调参、校准、A/B 三标签 | ✅ 已运行 |
| 数据 | `DataPanel.tsx` | K线、外部数据、同步三标签 | ✅ 已运行 |
| 系统 | `SystemPanel.tsx` | 报告、配置、任务三标签 | ✅ 已运行 |

### 1.2 后端已有模块但前端无视图（Phase 5/7 成果）

| 模块 | 后端文件 | 当前暴露 API | 缺失 |
|------|---------|-------------|------|
| VaR/CVaR | `risk/var.py` | ❌ 无 API | 前端无风险面板 |
| Kelly | `risk/kelly.py` | ❌ 无 API | 前端无仓位建议 |
| 压力测试 | `risk/stress_test.py` | ❌ 无 API | 前端无压力场景 |
| 集中度 | `risk/concentration.py` | ❌ 无 API | 前端无暴露监控 |
| 跨品种风险 | `risk/cross_asset.py` | ❌ 无 API | 前端无协方差视图 |
| 自动恢复 | `monitor/auto_recovery.py` | ❌ 无 API | 前端无恢复心跳 |
| 告警规则 | `monitor/alert_rules.py` | ❌ 无 API | 前端无告警历史 |
| 实验跟踪 | `research/experiment_tracker.py` | ❌ 无 API | 前端无实验列表 |
| 周报生成 | `research/report_generator.py` | ❌ 无 API | 前端无周报浏览 |
| 向量化回测 | `alpha/backtest/vectorized.py` | `backtest.py` 有 Job API | 前端无回测触发面板 |

### 1.3 审计遗留前端问题（v10）

| ID | 文件 | 问题 | 状态 |
|----|------|------|------|
| P0-9 | `MainDashboard.tsx:264` | drawdown 未乘 100 | ✅ 已修复 |
| P0-7 | `DataPanel.tsx:320` | `fetch` 非 `authFetch` | ✅ 已修复 |
| P0-8 | `MainDashboard.tsx:165` | `r.json()` 不检查 `r.ok` | ✅ 已修复 |
| TD5 | 前端 8+ 处 | polling 缺少 AbortController | ⚠️ 待修复 |
| TD6 | 前端 | 死导入清理 (50+ 处) | ⚠️ 待修复 |
| P2-1 | 多个文件 | 无 AbortController | ⚠️ 待修复 |

---

## 2. 新页面设计

### 2.1 页面总览

在主仪表盘底部功能按钮区新增 **3 个入口**（现有 5 个 → 8 个），将详情面板从 5 个扩展为 8 个：

| 新增 # | 图标 | 名称 | 说明 | 来源 |
|--------|------|------|------|------|
| 6 | 🛡️ | 风控 | VaR·Kelly·压力·集中度 | Phase 5 |
| 7 | 🚨 | 运维 | 告警·恢复·心跳·周报 | Phase 7 |
| 8 | 📊 | 回测 | 向量回测·历史任务·权益曲线 | Phase 1 |

> 设计原则：新增卡片优先嵌入现有 grid（8 按钮分两行 4+4 或单行 8 个缩放），避免独占整行挤压其他卡片。

---

### 2.2 风控面板（RiskPanel）

**文件**: `frontend-v2/src/components/panels/RiskPanel.tsx`

**标签页**: [总览] [VaR] [压力] [集中度]

#### 总览标签

顶部 4 个 KPI 卡片（嵌入 grid，单行 4 列）：

| 卡片 | 指标 | 数据来源 | 样式 |
|------|------|---------|------|
| VaR (95%) | `var_95` | `risk/var.py` → API | 正常=绿，>账户5%=红 |
| Kelly 建议 | `kelly_lot` | `risk/kelly.py` → API | 半凯利显示 |
| 压力状态 | `stress_passed` | `risk/stress_test.py` → API | 通过=绿，失败=红 |
| 集中度 | `max_type_pct` | `risk/concentration.py` → API | >40%=红 |

中间区域：风险仪表盘（RiskGauge）—— 用 DualRing 改制的综合风险分（0-100），绿色 0-30，黄色 30-70，红色 70-100。

底部：当前持仓风险摘要（从 `/api/live/positions` 复用，加风险列）。

#### VaR 标签

- 参数选择：置信度 95% / 99%，方法 parametric / historical / Monte Carlo
- 主数值：大字体 VaR（$），CVaR（$）
- 图表：VaR 历史滚动（最近 50 根 bar 的 VaR 值 MiniAreaChart）
- 说明文字："VaR 基于最近 500 根 bar 计算，Monte Carlo 用 10,000 次模拟"

#### 压力测试标签

- 场景列表（表格）：

| 场景 | 触发条件 | 预期损失 | 账户影响 | 状态 |
|------|---------|---------|---------|------|
| 黑天鹅 | 5σ 波动 | `$X` | `Y%` | 通过/未通过 |
| NFP 事件 | 非农数据 | `$X` | `Y%` | 通过/未通过 |
| 流动性枯竭 | 价差 > 20bps | `$X` | `Y%` | 通过/未通过 |
| 断线 30s | 无法下单 | `$X` | `Y%` | 通过/未通过 |
| 因子集体失效 | IC < 0 | `$X` | `Y%` | 通过/未通过 |

- 运行按钮："运行压力测试" → POST `/api/risk/stress/run` → 轮询 job

#### 集中度标签

- 饼图/环形图：因子类型权重分布（量价/动量/均值回归/波动率/非线性/ML）
- 阈值线：40% 红色警戒线
- 跨品种矨阵：XAUUSD+ / EURUSD 协方差热图（简化版文字表格）

---

### 2.3 运维面板（OpsPanel）

**文件**: `frontend-v2/src/components/panels/OpsPanel.tsx`

**标签页**: [告警] [恢复] [周报] [实验]

#### 告警标签

- 顶部：当前告警状态（Healthy / Warning / Critical）大徽章
- 表格：最近 20 条告警记录

| 时间 | 规则 | 级别 | 消息 | 状态 |
|------|------|------|------|------|
| 18:05 | 权益回撤 > 5% | warning | 回撤 5.2% | 已恢复 |
| 17:30 | 连续亏损 3 次 | warning | 连亏触发 | 活跃 |
| 16:00 | cTrader 断开 | critical | 断线 30s | 已恢复 |

- 底部：6 条规则配置状态（启用/禁用，阈值）
  - 权益回撤 > 5%
  - 连续亏损 3 次
  - 单因子权重 > 40%
  - cTrader 断开 > 30s
  - 数据同步延迟 > 30min
  - VaR 95% > 账户 5%

#### 恢复标签

- 心跳状态：AutoRecovery 30s 心跳指示灯（绿/红）
- 最近重启记录：

| 时间 | 原因 | 重启次数 | 状态 |
|------|------|---------|------|
| 18:00 | live loop 无心跳 | 1 | 成功 |
| 12:30 | scheduler 卡住 | 1 | 成功 |

- 手动触发："发送心跳测试" / "强制重启 loop" 按钮

#### 周报标签

- 顶部：最近生成时间 + 生成按钮
- 列表：历史周报文件（从 `/api/reports?kind=txt` 过滤 `weekly_*.md`）
- 预览区：Markdown 渲染（右侧，同 SystemPanel 报告浏览器）
- 7 段摘要：因子健康 / 归因 / ML / 持仓风险 / 市场环境 / 实验 / 因子库演进

#### 实验标签

- 表格：ExperimentTracker 本周记录

| ID | 类型 | 状态 | 时间 | 关键指标 |
|----|------|------|------|---------|
| exp-001 | 因子发现 | done | 06-15 | IC=0.032 |
| exp-002 | 权重自适应 | done | 06-15 | Sharpe=1.2 |

- 数据来源：后端新增 `/api/experiments` API

---

### 2.4 回测面板（BacktestPanel）

**文件**: `frontend-v2/src/components/panels/BacktestPanel.tsx`

**标签页**: [回测] [历史] [对比]

#### 回测标签

- 参数配置（Card）：
  - 品种：XAUUSD+（默认）
  - 周期：M15 / M5 / H1
  - risk_per_trade_pct：0.5 / 1.0 / 1.5 / 2.0
  - 启用熔断：开关
  - 数据长度：默认 202,865 bars（可下拉）
- 运行按钮："启动回测" → POST `/api/backtest/run` → job_id
- 进度条：`useJobPolling` 轮询
- 结果区：
  - 权益曲线（复用 `EquityCurve` 组件）
  - 指标卡片：总收益率 / 年化夏普 / 最大回撤 / 胜率 / 盈亏比 / 卡玛比率
  - 交易统计：交易次数 / 平均持仓 bar 数 / 月度收益（12 格热力图占位）

#### 历史标签

- 表格：历史回测任务列表

| ID | 时间 | 周期 | 风险% | 收益 | 夏普 | 回撤 | 状态 |
|----|------|------|------|------|------|------|------|
| bt-001 | 06-15 | M15 | 1.0 | 3.2% | 1.4 | 2.1% | done |

- 数据来源：GET `/api/backtest?status=done`

#### 对比标签

- 选择 2 个历史回测结果
- 对比表格：指标 A vs B
- 权益曲线双轴叠加

---

## 3. 主仪表盘增强

在 `MainDashboard.tsx` 现有 grid 中嵌入以下新增卡片，**不独占整行**：

### 3.1 风险速览卡片（嵌入 Row 2）

在"系统总览"和"权益曲线"之间插入（md:grid-cols-3 改为 md:grid-cols-4，调整比例）：

```
[系统总览] [风险速览] [权益曲线] [日志+策略]
```

**风险速览内容**：
- 小字标题："风控状态"
- 3 行指标：
  - VaR 95%：`$X`（正常绿 / 超限红）
  - Kelly：`0.XX lot`（建议仓位）
  - 压力测试：`5/5 通过`（全绿）
- 底部链接："查看详情 →" → 打开 RiskPanel

> 卡片高度与系统总览一致，使用 `GlassCard` 统一风格。

### 3.2 因子权重卡片增强（已有）

- 现有 Top 10 权重列表 → 增加类型颜色标记（量价=蓝，动量=绿，均值回归=黄，波动率=紫，ML=橙）
- 底部增加："类型集中度: XX%" 进度条

### 3.3 归因概览卡片增强（已有）

- 增加：DSR（Deflated Sharpe Ratio）数值
- 增加：NW-HAC 调整后的夏普值
- 增加：McFadden R²（拟合度）

---

## 4. 后端 API 新增设计

前端需要以下新增后端 API（均需 `RequireUser`）：

### 4.1 风险 API

```
GET  /api/risk/summary          → 风险总览（VaR/Kelly/压力/集中度）
GET  /api/risk/var?method=mc&confidence=0.95 → VaR/CVaR 计算
POST /api/risk/stress/run       → 启动压力测试 job
GET  /api/risk/stress/{id}      → 压力测试状态
GET  /api/risk/stress/latest    → 最近压力测试结果
GET  /api/risk/concentration    → 集中度分析
```

### 4.2 运维 API

```
GET  /api/ops/alerts?limit=20   → 最近告警记录
GET  /api/ops/alerts/rules      → 6 条规则配置状态
GET  /api/ops/recovery/status   → AutoRecovery 心跳状态
GET  /api/ops/recovery/history  → 最近重启记录
POST /api/ops/recovery/heartbeat→ 手动触发心跳测试
GET  /api/ops/reports/weekly    → 周报列表（过滤 weekly_*.md）
GET  /api/experiments?since=... → 实验记录
```

### 4.3 回测 API（已有，需补充）

```
GET  /api/backtest?status=done  → 历史回测列表（已有）
GET  /api/backtest/{id}         → 单个回测详情（已有）
GET  /api/backtest/{id}/report  → 回测报告（ equity curve + stats ）
```

---

## 5. 数据流与轮询设计

| 面板 | 数据端点 | 轮询频率 | 缓存策略 |
|------|---------|---------|---------|
| RiskPanel 总览 | `/api/risk/summary` | 30s | `useApi` 30s 缓存 |
| RiskPanel VaR | `/api/risk/var` | 60s | 手动刷新 |
| RiskPanel 压力 | `/api/risk/stress/latest` | 300s | 手动刷新 |
| OpsPanel 告警 | `/api/ops/alerts` | 10s | `useApi` 10s 缓存 |
| OpsPanel 恢复 | `/api/ops/recovery/status` | 10s | `useApi` 10s 缓存 |
| OpsPanel 周报 | `/api/ops/reports/weekly` | 300s | 手动刷新 |
| OpsPanel 实验 | `/api/experiments` | 300s | 手动刷新 |
| BacktestPanel 历史 | `/api/backtest?status=done` | 30s | `useApi` 30s 缓存 |

---

## 6. 前端组件复用与新增

### 复用组件

| 组件 | 来源 | 使用位置 |
|------|------|---------|
| `GlassCard` | `dashboard/GlassCard.tsx` | 所有新增卡片 |
| `KpiCard` | `dashboard/KpiCard.tsx` | Risk 总览 4 卡 |
| `DualRing` | `dashboard/DualRing.tsx` | 风险综合分 |
| `MiniAreaChart` | `dashboard/MiniAreaChart.tsx` | VaR 历史 |
| `EquityCurve` | `charts/EquityCurve.tsx` | 回测结果 |
| `Table` | `ui/Table.tsx` | 所有表格 |
| `Badge` | `ui/Badge.tsx` | 状态标记 |
| `ProgressBar` | `ui/ProgressBar.tsx` | 回测进度 |
| `TabBar` | 各 Panel 内联 | 所有新增 Panel |
| `useApi` | `hooks/useApi.ts` | 数据获取 |
| `useJobPolling` | `hooks/useJobPolling.ts` | 回测/压力测试 |

### 新增组件

| 组件 | 文件 | 说明 |
|------|------|------|
| `RiskGauge` | `dashboard/RiskGauge.tsx` | 风险综合分仪表盘（0-100） |
| `HeatMap` | `charts/HeatMap.tsx` | 跨品种协方差简化热图（文字色阶） |
| `AlertRow` | `dashboard/AlertRow.tsx` | 告警行（带级别色条） |
| `HeartbeatIndicator` | `dashboard/HeartbeatIndicator.tsx` | 30s 心跳脉冲动画 |

---

## 7. UI 规范（符合用户偏好）

| 项 | 规范 |
|----|------|
| 字体 | 最小 `text-[11px]`，标题 `text-xs`，KPI 数值 `text-sm font-bold` |
| 中文 | 所有 UI 文字全中文，英文缩写后跟中文括号 |
| 数字标签 | 调度任务数字带中文：`已执行 3 次`、`5 次错误`、`每 10 分钟` |
| cron | 中文化：`每小时整点`、`每 30 分钟`、`每 5 分钟` |
| 颜色 | 沿用 tailwind 配置：`up`/`down`/`warn`/`accent` |
| 布局 | 新增卡片嵌入现有 grid，不独占整行 |
| 动画 | 保持 `glass-hover` 统一动效 |

---

## 8. 实现路线

### Phase A: 后端 API（先）

1. `backend/api/risk.py` — 封装 `risk/` 模块为 REST API
2. `backend/api/ops.py` — 封装 `monitor/` + `research/` 为 REST API
3. `backend/api/experiments.py` — 封装 `research/experiment_tracker.py`
4. 补充 `backend/api/backtest.py` — `/report` 端点

### Phase B: 前端组件（后）

1. `RiskPanel.tsx` — 4 标签页
2. `OpsPanel.tsx` — 4 标签页
3. `BacktestPanel.tsx` — 3 标签页
4. `MainDashboard.tsx` — 嵌入风险速览卡片 + 增强权重/归因
5. `App.tsx` — 路由无需改动（SlidePanel 模式）

### Phase C: 调度器接入

- 回测任务、压力测试任务注册到 `InProcessScheduler`，前端 `JOB_LABELS` 可见
- 周报生成 `weekly_report` cron job（周日）

---

## 9. 验证清单

- [ ] 新增 3 面板在 8 按钮 grid 中正确排列
- [ ] 所有新增数据调用使用 `authFetch`，非裸 `fetch`
- [ ] 所有 polling 添加 `AbortController`（修复 TD5）
- [ ] 所有 `r.ok` 检查到位（修复 P0-8 同类问题）
- [ ] 字体 ≥ 11px，无 `text-[9px]`
- [ ] 数字带中文标签，cron 中文化
- [ ] 回测/压力测试任务启动时立即补跑一次（scheduler catch-up）
- [ ] 新增任务注册到 `JOB_LABELS` 和 `CRON_LABELS`

---

> 本设计文档为动态草案，需确认后进入实施。确认点：
> 1. 3 个新增面板是否覆盖当前需求？
> 2. 主仪表盘风险速览卡片位置是否合适？
> 3. 后端 API 设计是否满足前端数据需求？
> 4. 实现优先级（先做后端 API 还是先前端 mock）？

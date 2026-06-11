# Quant Trading Web Console — Frontend Architecture

## 设计理念

单页仪表盘应用，所有功能悬浮在主视图之上。无页面跳转、无侧边栏导航，所有操作通过下滑面板完成。

## 核心结构

```
frontend-v2/src/
├── App.tsx                    # 根组件，仅处理登录状态
├── globals.css                # 全局样式（毛玻璃、动画）
├── lib/
│   ├── store.ts               # Zustand 全局状态（WS 快照 + 认证）
│   ├── auth.ts                # JWT 认证
│   ├── ws.ts                  # WebSocket 客户端
│   ├── format.ts              # 格式化工具
│   ├── theme.ts               # 图表主题常量
│   └── hooks/                 # 自定义 Hooks
│       ├── useAliveRef.ts     # 组件存活引用
│       ├── usePolling.ts      # 轮询
│       ├── useApi.ts          # API 请求
│       ├── useConfirm.ts      # 确认对话框
│       └── useJobPolling.ts   # 任务轮询
├── pages/
│   ├── Login.tsx              # 登录页
│   └── MainDashboard.tsx      # 仪表盘主页（核心入口）
├── components/
│   ├── dashboard/             # 仪表盘组件
│   │   ├── GlassCard.tsx      # 毛玻璃卡片容器
│   │   ├── KpiCard.tsx        # KPI 指标卡
│   │   ├── DualRing.tsx       # 双环仪表（胜率+回撤）
│   │   ├── MiniAreaChart.tsx  # 迷你面积图
│   │   ├── ProgressBar.tsx    # 水平进度条
│   │   ├── FunctionButton.tsx # 渐变功能按钮
│   │   └── SlidePanel.tsx     # 下滑面板容器
│   ├── panels/                # 5 个下滑面板
│   │   ├── TradingPanel.tsx   # 交易（模拟盘+实盘）
│   │   ├── FactorsPanel.tsx   # 因子（健康+发现+影子）
│   │   ├── ExperimentsPanel.tsx # 实验（调参+校准+A/B）
│   │   ├── DataPanel.tsx      # 数据（K线+同步）
│   │   └── SystemPanel.tsx    # 系统（报告+配置+任务）
│   ├── charts/                # 图表组件
│   │   ├── Candlestick.tsx    # K线图
│   │   └── EquityCurve.tsx    # 权益曲线
│   └── ui/                    # 通用 UI 组件（面板内使用）
│       ├── Button.tsx, Card.tsx, Badge.tsx, Table.tsx ...
│       └── index.ts
└── tailwind.config.ts         # 毛玻璃主题配置
```

## 视觉设计

- **背景**: 浅灰渐变 `#f5f7fa → #e4e9f0`
- **卡片**: 毛玻璃效果 `rgba(255,255,255,0.6)` + `backdrop-filter: blur(20px)`
- **仪表元素**: 环形图、SVG 面积图、水平进度条、迷你 Sparkline 混排
- **功能入口**: 5 色渐变按钮（蓝/绿/橙/紫/灰）
- **交互**: 悬停上浮+缩放+蓝光阴影，下滑面板 cubic-bezier 动画

## 数据流

1. WebSocket 每秒推送账户快照到 Zustand store
2. 仪表盘从 store 读取实时数据（Equity/PnL/持仓/风控）
3. 下滑面板内的子功能通过 `authFetch` 调用 REST API
4. 任务（回测/发现/调参）使用 `useJobPolling` hook 轮询状态

## 后端 API 映射

| 前端操作 | API 端点 |
|---------|---------|
| 登录 | POST /api/auth/login |
| WS 快照 | ws://…/ws/state |
| 模拟盘控制 | /api/paper/* |
| 实盘控制 | /api/live/* |
| 因子健康 | /api/factor-health/* |
| 因子发现 | /api/discover/* |
| 影子因子 | /api/shadow/* |
| 调参 | /api/tuning/* |
| 校准 | /api/calibrator/* |
| A/B测试 | /api/ab/* |
| K线数据 | /api/market/* |
| 同步 | /api/sync/* |
| 报告 | /api/reports/* |
| 配置 | /api/config/* |
| 任务 | /api/jobs/* |

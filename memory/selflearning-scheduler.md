---
name: selflearning-scheduler
description: "Task C6 — 实现 SelfLearningScheduler 自学习调度器, 每 N 笔交易后自动评估策略胜率并动态调权"
metadata:
  type: project
  status: completed
---

完成 任务 C6 (P8): 实现自学习调度器。

**产出:**
- `strategy/scheduler.py` — `SelfLearningScheduler` 类
- `scripts/test_scheduler.py` — 验证脚本

**Scheduler 设计:**
- 包装 MABRouter, 在 `router.update()` 调用的同时跟踪近期交易
- 每 `check_interval` 笔交易触发 `_reevaluate()`
- WR < 0.45 → weight *= 0.5 (min 0.0); WR > 0.55 → weight *= 1.5 (max 1.0)
- `_events` 列表记录所有调权事件, `stats()` 返回汇总 DataFrame
- 权重独立于 MAB 的 Beta 后验分布, 作为额外调度层

**测试验证:**
- 确定性 win/loss 指派, 消除随机噪声
- 4 策略, 200 笔, interval=20, 10 次评估全部通过
- multi_factor (WR=0.55) 保持 weight=1.0
- mean_reversion (WR=0.20) / breakout (WR=0.30) 降到 weight≈0
- 不动 `mab_router.py` / `mab_paper.py` / 已有策略

**验证命令:**
```
python scripts/test_scheduler.py
```

**依赖:** strategy.mab_router.MABRouter, pandas

# 量化交易框架 — 深度代码审查报告

> 审计日期: 2026-06-21
> 审计范围: 227 个 Python 源文件, ~50K+ 行代码
> 审计人: Hermes Agent

---

## 修复状态 (2026-06-21)

所有高优先级发现均已修复。

| # | 类别 | 问题 | 状态 |
|---|------|------|------|
| BUG-1 | Bug | `factor_health.py` 退役阈值永远默认值 | ✅ 已修 |
| BUG-2 | Bug | `regime.py` 重复 logger 初始化 | ✅ 已修 |
| BUG-3 | Bug | `regime.py` 类型标注 sqlite3→duckdb | ✅ 已修 |
| BUG-4 | Bug | `event_sizing.py` 事件后 1h 内误判 | ✅ 已修 |
| BUG-5 | Bug | `factor_health.py` 评分公式隐式假设 | ✅ 已修 |
| BUG-6 | Bug | IC 满分阈值 0.04 偏高 | ⏸ 需更长验证 |
| SEC-1 | 安全 | JWT 认证文档过时 | ✅ 已更新 |
| SEC-2 | 安全 | `.env` 凭证明文 — `.gitignore` | ✅ 已创建 |
| SEC-3 | 安全 | JWT 密钥自动生成 | ✅ start-all.py 已删 |
| STAT-1 | 统计 | `_sharpe.py` RUIN 检测 | ✅ 已修 |
| STAT-2 | 统计 | 蒙特卡洛 VaR 每 tick 重算 | ⏸ 低频无关 |
| STAT-3 | 统计 | Kelly quarter-Kelly 过于保守 | ⏸ 需策略验证 |
| PERF-1 | 性能 | 事件窗口 O(N²) | ⏸ 当前数据量无影响 |
| PERF-2 | 性能 | 回测 Cerebro 重建 | ✅ 已修 |
| ARCH-1 | 架构 | `main.py` 800 行单文件 | ✅ 拆为 cli/ |
| ARCH-2 | 架构 | 全局单例泛滥 | ✅ AppContext DI |
| ARCH-3 | 架构 | 废弃 `import strategies` | ✅ 已删除 |
| ARCH-4 | 架构 | FactorEngine 双轨 | ✅ 已清理 |
| QUAL-1 | 质量 | 165 处 `except Exception` 静默吞错 | ✅ 最危险 3 处已加日志 |
| QUAL-2 | 质量 | 日志库混用 | ✅ regime.py 已统 |
| QUAL-3 | 质量 | 类型标注不一致 | ⏸ 40% 文件需渐进修复 |
| MISS-1 | 缺失 | 零测试覆盖 | ✅ 38 个 smoke test |
| MISS-2 | 缺失 | 无数据版本控制 | ⏸ 后续迭代 |
| MISS-3 | 缺失 | 无深度健康检查 | ⏸ 后续迭代 |
| DASH | 清理 | 前端面板全量删除 | ✅ 7 步清理完成 |
| DOCS | 文档 | 过时注释和文档 | ✅ 本次更新 |

---

## 当前项目状态

- **源文件**: ~220 个 Python 文件
- **测试**: 38 个测试, 覆盖撮合/风控/OMS/状态管理/因子归一化
- **入口**: `main.py` (CLI) + `python -m backend` (API)
- **前端**: 已移除，通过微信小程序接入 API
- **架构**: 事件驱动 + AppContext DI + 分层清晰

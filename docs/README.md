# Documentation Index

当前文档集只保留正在维护的入口文档。旧蓝图、旧 Web Console 设计、早期 proposal 和生成报告已清理，避免同一主题多份互相冲突。

## 主要文档

- [../README.md](../README.md) - 项目总览和快速入口
- [../TODO.md](../TODO.md) - 当前状态、收尾事项和技术债
- [../CLAUDE.md](../CLAUDE.md) - Codex/Claude 协作上下文和工程约定
- [startup.md](startup.md) - 后端、小程序和常用脚本启动方式
- [development-workflow.md](development-workflow.md) - 本地/GitHub/服务器三端开发、发布和同步流程
- [architecture.md](architecture.md) - 当前系统架构、风控/学习/模型边界和未来完全体
- [CTRADER_INTEGRATION.md](CTRADER_INTEGRATION.md) - cTrader 执行通道说明
- [planning/self-evolving-upgrade-plan.md](planning/self-evolving-upgrade-plan.md) - 规则学习与模型数据闭环设计
- [planning/multi-symbol-pipeline.md](planning/multi-symbol-pipeline.md) - 多品种管道规划
- [../miniprogram_v2/README.md](../miniprogram_v2/README.md) - 当前微信小程序前端

## 文档维护规则

- 新增长期文档前先检查本索引，避免重复主题。
- 当前运行口径以 `README.md`、`docs/startup.md`、`docs/architecture.md`、`TODO.md` 为准。
- 历史设计不要重新放回 `docs/` 主目录；需要保留时先提炼到当前主文档。

## 三端协作规则

- 完整流程见 [development-workflow.md](development-workflow.md)。
- 简要原则：本地是主开发端，GitHub `main` 是最终合并源，服务器是后端真实运行和验证端。
- 服务器允许短事务热修，但必须立即 commit / push GitHub，再从 GitHub 下发到本地和服务器，保持三端一致。

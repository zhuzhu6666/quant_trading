# Documentation Index

当前文档集只保留正在维护的入口文档。旧蓝图、旧 Web Console 设计、早期 proposal 和生成报告已清理，避免同一主题多份互相冲突。

## 主要文档

- [../README.md](../README.md) - 项目总览和快速入口
- [../TODO.md](../TODO.md) - 当前状态、收尾事项和技术债
- [../CLAUDE.md](../CLAUDE.md) - Codex/Claude 协作上下文和工程约定
- [startup.md](startup.md) - 后端、小程序和常用脚本启动方式
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

- 后端代码以服务器修改和验证为准。
- 前端代码以本地 `miniprogram_v2` 修改和验证为准。
- GitHub `main` 是最终合并源。
- 每轮发布后统一执行：本地提交 -> 推送 GitHub -> 同步服务器 -> 必要时从 GitHub 下发到本地和服务器，保持本地、GitHub、服务器三端一致。

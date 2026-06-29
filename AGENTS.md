# Workspace Rules

> Last updated: 2026-06-29

这个仓库从现在开始按下面的规则协作：

## 1. 本地 Windows

本地默认只做这些内容：

- `miniprogram_v2`
- 小程序页面、交互、展示
- 微信开发者工具验证
- 文档

本地默认不要改这些内容：

- `backend`
- `execution`
- `alpha`
- `risk`
- `monitor`
- `.env`
- `config`
- `data`
- `logs`

## 2. Linux 服务器

服务器默认负责这些内容：

- 后端接口
- 交易循环
- 风控逻辑
- cTrader 执行链路
- 环境变量
- systemd
- 数据库
- 日志排查

服务器默认不做这些内容：

- 小程序页面开发
- 微信开发者工具联调

## 3. 默认工作流

```text
本地只做小程序
服务器只做后端
```

## 3.1 当前分支/工作区约定

- 本地 Windows 默认使用 `miniprogram-main` 分支。
- 本地 Windows 已启用 sparse checkout，默认只保留：
  - `miniprogram_v2/`
  - `docs/`
  - `AGENTS.md`
  - `README.md`
  - `.gitignore`
- Linux 服务器默认使用 `main` 分支。
- 后端、交易、数据库、systemd、日志相关改动一律在服务器 `main` 上完成。
- 小程序改动一律在本地 `miniprogram-main` 上完成。
- 文档/规则类改动如影响双方，需要同时同步到 `main` 和 `miniprogram-main`。

## 3.2 当前数据约定

- tick 数据在服务器上按月库保存：
  - `data/ticks_monthly/ticks_YYYY_MM.duckdb`
  - `data/ticks.duckdb` 是指向当前月份库的兼容链接
- L2 数据在服务器上由独立服务采集：
  - `quant-l2-collector.service`
  - 数据库：`data/l2.duckdb`
- 这些运行数据不进入 GitHub。

## 4. 遇到问题时的默认顺序

```text
先看日志
  -> 再看接口
  -> 再改代码
  -> 最后重启验证
```

## 5. 详细规则

完整说明见：

- [docs/development-workflow.md](C:/Users/zhu/quant_trading/docs/development-workflow.md)
- [docs/server-backend-sop.md](C:/Users/zhu/quant_trading/docs/server-backend-sop.md)

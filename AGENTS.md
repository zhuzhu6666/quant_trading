# Workspace Rules

> Last updated: 2026-06-26

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

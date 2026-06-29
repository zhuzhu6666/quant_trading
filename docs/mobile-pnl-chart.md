# Mobile PnL Chart

> Last updated: 2026-06-29

当前小程序收益图已经不再走 `web-view` / nginx 静态 H5 方案。

## Current Implementation

当前实现是微信小程序原生页面：

```text
miniprogram_v2/pages/pnl-chart/index
```

入口在总览页：

```text
miniprogram_v2/pages/overview/index.wxml
```

页面使用本地 vendored `uCharts`：

```text
miniprogram_v2/vendor/ucharts/u-charts.min.js
```

这样做是因为当前小程序没有 `web-view` 业务域名配置权限，不能依赖微信公众平台配置 `www.zhuzhu666.icu` 作为 web-view 业务域名。

## Data Contract

图表数据仍来自后端接口：

```text
GET /api/live/realized-pnl-series?scope=all
```

小程序通过当前登录态和已有 request 合法域名访问后端 API，不通过 H5 页面转发，也不需要单独配置 web-view 域名。

## Deprecated Plan

以下方案已经废弃，不要再作为当前状态判断依据：

- `https://www.zhuzhu666.icu/mobile/pnl-chart/`
- nginx `location /mobile/`
- nginx `location /vendor/` 为 TradingView/lightweight-charts H5 提供静态资源
- 微信公众平台配置 `www.zhuzhu666.icu` 为 web-view 业务域名
- H5 内置 TradingView `lightweight-charts`

这些内容只代表历史尝试，不代表当前小程序实现。旧的 `server_static/mobile/pnl-chart/` 和 `server_static/vendor/lightweight-charts/` 静态文件已经从仓库移除。

## Verification Notes

当前验证入口应放在微信开发者工具或真机小程序内：

1. 打开 `miniprogram_v2`
2. 登录后进入总览页
3. 点击“打开专业收益图”
4. 确认进入 `/pages/pnl-chart/index`
5. 确认图表能展示 `/api/live/realized-pnl-series?scope=all` 返回的数据

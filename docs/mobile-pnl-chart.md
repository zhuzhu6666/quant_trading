# Mobile PnL Chart H5

小程序的“TV 图表”入口会打开：

```text
https://www.zhuzhu666.icu/mobile/pnl-chart/
```

该页面是 nginx 静态 H5，不经过 FastAPI 渲染。页面内置 TradingView
`lightweight-charts`，同域请求：

```text
/api/live/realized-pnl-series?scope=all
```

## Server deploy

1. 拉取最新代码。

```bash
cd /opt/quant_trading
git pull --ff-only
```

2. 在 nginx 站点配置里加入静态目录。

```nginx
location /mobile/ {
    alias /opt/quant_trading/server_static/mobile/;
    try_files $uri $uri/ =404;
}

location /vendor/ {
    alias /opt/quant_trading/server_static/vendor/;
    try_files $uri =404;
    access_log off;
    expires 30d;
    add_header Cache-Control "public, max-age=2592000, immutable";
}
```

如果服务器仓库路径不是 `/opt/quant_trading`，把 `alias` 改成真实路径。

3. 检查并重载 nginx。

```bash
nginx -t
systemctl reload nginx
```

4. 确认 H5 可访问。

```text
https://www.zhuzhu666.icu/mobile/pnl-chart/
```

直接浏览器打开时会提示从小程序进入，因为图表 API 需要小程序传入 JWT。

## WeChat setup

微信公众平台需要把下面域名加入 web-view 业务域名：

```text
www.zhuzhu666.icu
```

后端 API 域名仍然保持现有 request 合法域名配置。

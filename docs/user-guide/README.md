# 旧 Web Console 用户手册

> 状态: 已归档，保留作历史参考。
> 最后清理: 2026-06-25

这个目录原来描述的是 Vite/React 浏览器 Web Console。当前代码主线已经切换为:

- 后端: `python -m backend`
- 前端: `miniprogram_v2`
- 权威启动文档: `docs/startup.md`
- 当前前端说明: `miniprogram_v2/README.md`

不要再按旧文档里的 `start-all.py`、`:5173`、`frontend-v2/src` 或 Web 路由说明启动系统；这些内容对应旧浏览器控制台，不再是维护入口。

## 当前操作入口

1. 启动后端:

```bash
python -m backend
```

2. 用微信开发者工具打开:

```text
C:\Users\zhu\quant_trading\miniprogram_v2
```

3. 回测、模拟盘、外部数据刷新等命令见:

```text
docs/startup.md
```

## 保留原因

旧 Web Console 的页面设计、轮询模式和部分 API 映射仍可作为历史实现参考，但不能作为当前开发、部署或验收依据。

## MemPalace install log

- 2026-06-07: Task 1 完成 — CLI 已装 (MemPalace 3.4.0)
- 路径: C:\Users\zhu\.local\bin\mempalace.exe
- 副可执行: mempalace-mcp (同目录)
- uv: 0.11.19, Python: 3.11.15 (active venv), 3.12 (PATH)
- 磁盘可用: 520G (~ /c)

### Step 4 deviation note

`uv tool uninstall mempalace --dry-run` 在 uv 0.11.19 不支持 (`error: unexpected argument '--dry-run'`)。
改用 `uv tool list` 确认 rollback 路径:

```
$ uv tool list | grep mempalace
mempalace v3.4.0
- mempalace
- mempalace-mcp
```

真要回滚时跑 `uv tool uninstall mempalace` (无 dry-run)。

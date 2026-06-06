## Task 6: schtasks 计划任务已注册

### 结果摘要

- **任务名**: `MempalacePeriodicSave`
- **周期**: 每 30 分钟 (`/sc minute /mo 30`)
- **身份**: 当前用户 `DESKTOP-0GAV3JD\zhu`,最高权限
- **执行命令**: `C:\Users\zhu\quant_trading\.claude\hooks\run-periodic-save.bat`
- **下次运行**: 2026/6/7 3:15:00 (注册时)

### 关键偏差(从 plan)

1. **schtasks 不接受 `bash` 作为可执行**——`/tr` 必须指向 .exe 或 .bat
   - 写了一个 .bat 包装器 `.claude/hooks/run-periodic-save.bat`,内部 cd 到项目根 + 设 `CLAUDE_PROJECT_DIR` + 调 bash 跑钩子
2. **路径空格问题**——`C:\Program Files\Git\usr\bin\bash.exe` 含空格,直接传给 schtasks 会被截断;用 .bat 包装器解
3. **守卫需要 `CLAUDE_PROJECT_DIR` env**——schtasks 启的子进程没自动设这个变量;.bat 里手动 `set CLAUDE_PROJECT_DIR=C:\Users\zhu\quant_trading`
4. **`date` 命令在 schtasks bash 里找不到**——Git Bash 的 PATH 缺 date;改用 bash 内建 `printf '%(%Y-%m-%dT%H:%M:%S%z)T' -1`,pre-compact.sh 和 periodic-save.sh 都改了

### 验证

- `schtasks /query /tn MempalacePeriodicSave /v /fo list` → 任务存在,下次 3:15:00
- `schtasks /run /tn MempalacePeriodicSave` → "成功:已启动任务"
- 等 25 秒后 `tail C:\Users\zhu\.mempalace\logs\periodic-save.log`:
  ```
  [2026-06-07T02:58:15+0800] Periodic sweep start
    Swept 59/59 files from C:\Users\zhu\.claude\projects\C--Users-zhu-quant-trading: +3 new, 47 already present, 9896 skipped (< cursor).
  [2026-06-07T02:58:27+0800] Periodic sweep done
  ```
- 多次手动 .bat + schtasks /run 触发都成功,每次 +N new drawer(增量幂等)

### 卸载命令

```cmd
schtasks /delete /tn MempalacePeriodicSave /f
```

完整卸载见 spec §9。

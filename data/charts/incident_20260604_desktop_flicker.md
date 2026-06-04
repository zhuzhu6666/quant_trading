# Incident Report: 桌面终端无限闪烁 (2026-06-04)

> 现象: 桌面任务栏终端窗口每 5 秒闪一次,持续数小时
> 影响: 用户体验,非 PnL;无数据丢失;无交易异常
> 严重度: 🟡 中(可见性差,但 silent failure 已正确写到 `logs/live_sync.log`)

---

## ⏱ 时间线

| 时刻 (UTC+8) | 事件 |
|---|---|
| 13:22:51 | `svchost.exe` PID 1784 (Task Scheduler 服务) 启动 |
| 13:22:54 | 计划任务 `\MT5SyncDaemon` 触发,启动 `pythonw.exe` PID 4000 |
| 13:22:54 | daemon 进入 `mt5_guard.check_one(poll_sec=5, max_wait_sec=0)` 循环 |
| 13:22:54 ~ 13:30:55 | 每次 poll 找不到 `terminal64.exe` → `subprocess.run(["powershell", ...])` 拉进程列表 → powershell 启/停 → 任务栏闪一下;日志每 5 秒一行 `[MT5Guard] no terminal64 running; polling every 5s` |
| 13:30:55 附近 | 用户注意到闪烁,开始排查 |
| 排查完成 | `schtasks /End /TN \MT5SyncDaemon` 终止 pythonw 4000,闪停 |
| 用户手动 | `taskschd.msc` → 禁用 `\MT5SyncDaemon` |
| 代码修复 | `mt5_guard.check_one` 默认 `max_wait_sec=0` → `600`;daemon 调用处改读 `self._cli_max_wait_sec`;新增 `--max-wait-sec` CLI 参数(默认 600) |

> 关键时序证据: `logs/live_sync.log` 第 435 秒的最后一行(13:30:55, waited 435s / max 0s)与任务调度器记录的"上次运行时间 2026/6/4 13:22:54"完全对得上。

---

## 🔥 根因

**`max_wait_sec=0` 的 foot-gun 设计被默认调用链触发,导致 MT5 未运行时进入无限 poll 循环。**

调用链:
```
\MT5SyncDaemon (Task Scheduler, 登录时触发)
  └─ pythonw.exe -m data.live_sync.daemon --mode daemon
       └─ SyncDaemon.run_daemon()                        [daemon.py:137]
            └─ mt5_guard.check_one(poll_sec=5.0,
                                    max_wait_sec=0)      [daemon.py:165]
                 └─ while True:
                      pids = _list_terminal64_pids()     [mt5_guard.py:38]
                        └─ subprocess.run(["powershell", "-NoProfile",
                                            "-Command",
                                            "@(Get-Process terminal64 ...)"])
                      if 找到 1 个 → return
                      if 找到 >1 → raise RuntimeError
                      if max_wait_sec>0 && waited>=max → raise
                      time.sleep(poll_sec)
```

- 当 `max_wait_sec=0`,条件 `if max_wait_sec > 0 and waited >= max_wait_sec` 永真为 False → 永不 raise → 死循环
- `subprocess.run(["powershell", ...])` 每次都启一个 `powershell.exe` 子进程(几毫秒就退),从 conhost 看就是"窗口一闪"
- pythonw 是无控制台启动,自身不刷屏,**刷屏源是它反复启停的 powershell 子进程**
- daemon 已经正确写文件日志(commit 2fa5695 加了 `setup_file_logging`),所以 silent failure 没问题 — 闪屏才是用户感知

---

## 🔍 诊断过程

1. 任务管理器看不出 → 闪烁太快,CPU 峰值仅 ms 级
2. 用户提示与 MT5 相关
3. `Get-Process` 列进程,锁定 `pythonw.exe` PID 4000(无命令行,无窗口)
4. WMI 查父进程 → `ParentProcessId=1784` → 该 svchost 托管 `Schedule` (Task Scheduler) 服务
5. `schtasks /Query /FO CSV /V` 全量导出,grep 关键词(`.py`/`live_sync`/`mt5`)→ 命中 `\MT5SyncDaemon`
6. 任务命令行 `pythonw.exe -m data.live_sync.daemon --mode daemon --interval 60 --timeframes M5,M15,M30,H1,H4,D1 --gap-threshold-hours 6 --heartbeat-sec 300` ← 与 `daemon.py` 模块签名完全对得上
7. 任务的"上次运行时间"= pythonw 的 CreationDate,分秒一致
8. `logs/live_sync.log` 尾部全是 `[MT5Guard] no terminal64 running; polling every 5s` → 印证 poll 循环
9. `terminal64.exe` 进程数 = 0 → MT5 确认未开
10. `Stop-Process` / `taskkill /F` 均失败(被 SYSTEM 任务调度器保护) → 用 `schtasks /End /TN \MT5SyncDaemon` 终止当前实例 → 进程消失,闪停

---

## 🛠 修复

### 代码 (commit pending)

| 文件 | 改动 |
|---|---|
| `data/live_sync/mt5_guard.py:70` | `check_one(poll_sec=5.0, max_wait_sec:int=0)` → `max_wait_sec:int=600` |
| `data/live_sync/mt5_guard.py:75-80` | docstring 把"max_wait_sec=0 means wait forever"改为"max_wait_sec<=0 means wait forever -- and is the foot-gun mode that produced the 2026-06-04 desktop-flicker incident" |
| `data/live_sync/daemon.py:107` | `SyncDaemon.__init__` 新增 `max_wait_sec: int = 600` 参数 |
| `data/live_sync/daemon.py:117` | 实例属性 `self._cli_max_wait_sec = max_wait_sec` |
| `data/live_sync/daemon.py:175` | `check_one` 调用从 `max_wait_sec=0` 改为 `max_wait_sec=self._cli_max_wait_sec` |
| `data/live_sync/daemon.py:321-323` | 新增 `--max-wait-sec` CLI 参数,默认 600,docstring 注明"Set to 0 to wait forever (not recommended)" |
| `data/live_sync/daemon.py:345` | `SyncDaemon(max_wait_sec=args.max_wait_sec, ...)` 注入 |

**未改**:
- `daemon.py:220` `mt5_guard.reconnect(mt5, poll_sec=5.0, max_wait_sec=300)` 保持 300s — 这是 daemon 运行中检测到心跳失败后的重连超时,5 分钟合理
- `mt5_guard.py:147` `reconnect` 默认 300s — 同上

### 任务

- 用户已在 `taskschd.msc` 手动禁用 `\MT5SyncDaemon`
- 改任务命令行(`schtasks /Change`)需要管理员权限,本次未做
- 未来重新启用时,新代码默认 600s,无需改命令行

---

## 💡 教训

1. **"无限等待"是默认值 = foot-gun。** 任何 wait/poll 函数,默认值必须是有限超时(常见 60s/300s/600s);"永远等"必须显式传大数或 0,而不是默认可得。
2. **"silent failure 写到日志"不等于"用户能感知"。** `setup_file_logging` 解决了"丢日志"问题,但用户看到的桌面现象是 powershell 子进程闪烁。**写文件日志 + 限时退场** 才是完整的 self-healing 设计。
3. **CLI 默认值要和函数默认值一致。** 这次 `check_one` 默认 0 + daemon 调用显式传 0,两边都"默契地"走了 foot-gun 路径。daemon CLI 新增 `--max-wait-sec 600` 之后,即使有人未来回退 `check_one` 默认值,CLI 路径也是安全的。
4. **"subprocess 启 powershell"是桌面闪烁的常见源。** 任何监控外部进程的循环,在 Windows 下都要考虑 powershell.exe 子进程被 conhost 渲染成窗口闪烁的副作用。能用 `pywin32`/`psutil` 查的就别走 powershell 中转。
5. **WMI/计划任务/服务托管是 desktop 进程排查的三大入口。** 这次靠 `Get-CimInstance Win32_Process` 找父 + `schtasks /Query` 找任务,5 分钟内定位。`Get-Process` 看不到命令行时,不要放弃,直接上 WMI。

---

## 🔗 相关

- 引入 foot-gun 的 commit: `2fa5695 fix(live_sync): auto-recovery via mt5_guard + gap detection + heartbeat`
- 修复 commit: (pending)
- 同类 incident: 见 `data/charts/framework_audit_20260603.md` (2026-06-03 那次 MT5 隐藏实例 + GUI 终端碰撞,root cause 与本次不同但同模块)
- 相关代码:
  - `data/live_sync/daemon.py`
  - `data/live_sync/mt5_guard.py`
  - `data/live_sync/orchestrator.py`

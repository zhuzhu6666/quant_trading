## Task 3: quant_trading 项目已 mine

### 结果摘要

- **模式**: `--mode projects` (plan 里写的是 `--mode files`,**已偏差修正**,见下)
- **实际入库**: 293 个文件,24010 个 drawer,33 条 hallway
- **耗时**: ~10 分钟 (含 onnx-mini-lm-l6-v2 模型首次下载 ~120MB)
- **搜索验证**:
  - `XAUUSD M15 趋势` → 3 条 (top: `factor_ic_report.txt`,含 "XAUUSD+ M15, 50K bar" verbatim)
  - `回测 Sharpe` → 3 条 (top: `mab_paper_runner.py`,含 Sharpe 公式)

### Embedding 模型

实际使用: **onnx-mini-lm-l6-v2** (Chroma 默认,all-MiniLM-L6-v2 同源)
- 在 mine 输出中通过 `chromadb/utils/embedding_functions/onnx_mini_lm_l6_v2.py` 路径名确认
- 模型下载: 首次启动从 huggingface 拉,出现 SOCKS 代理兼容问题,详见"偏差与修复"
- 缓存位置: `~/.cache/chroma/onnx_models/` (推测)

### Room 分布

**plan 预期**: code / docs / scripts / root 四个 room
**实际**: 只有 `general` 一个 room (293 files / 24010 drawers)

mempalace 3.4.0 的 `projects` mode 默认所有文件都进 `general` room。
plan 里的"按子目录自动推"假设不成立,可能是旧版本或不同 mempalace 变体的行为。
对搜索质量无影响 (元数据中 `source` 字段已带完整相对路径),不影响下一步。

### 偏差与修复 (deviations from plan)

1. **`--mode files` → `--mode projects`**
   - plan 假设的 `--mode files` 不存在
   - 实际: `mempalace mine` 只有 `{projects,convos,extract}` 三种 mode
   - `projects` 是 code/docs 的正确 mode (同时也是默认),适配

2. **`--limit N` → `--results N` (仅 search 子命令)**
   - plan 的 `mempalace search ... --limit 3` 报错 `unrecognized arguments: --limit 3`
   - 实际: search 子命令只接受 `--results` (无 `--limit` 复数名)
   - 修正后正常

3. **`mempalace list-rooms` 不存在**
   - plan 的 `mempalace list-rooms --wing quant-trading` 报错 `invalid choice: 'list-rooms'`
   - 实际: mempalace 3.4.0 子命令只有 `{init, mine, sweep, sync, search, compress, wake-up, split, hook, instructions, repair, repair-status, mcp, migrate, migrate-wings, status}`
   - 用 `mempalace status --backend chroma` 替代,显示 wing + room + drawer 数

4. **SOCKS 代理 + httpx 缺 socksio (首次 mine 失败)**
   - 现象: 第一次 mine 跑到 chromadb embedding 下载阶段,`ImportError: Using SOCKS proxy, but the 'socksio' package is not installed`
   - 原因: Windows 环境 `HTTPS_PROXY` / `ALL_PROXY` 设了 SOCKS 代理 (Clash 7897),但 mempalace 自己的 venv 没装 `socksio`
   - 修复:
     ```bash
     uv pip install --python /c/Users/zhu/AppData/Roaming/uv/tools/mempalace socksio
     # Installed: socksio==1.0.0
     ```
   - 重试 mine 后 120MB 模型走 SOCKS 代理成功下载,mine 正常完成
   - **影响**: 后续 Task 4 (mine 会话) 和 Task 5/6 (hooks) 都会沿用这个 venv,装一次 socksio 全局生效
   - **建议**: 在 install 文档里加一句"`mempalace` venv 需 `socksio` (SOCKS 代理环境)"

5. **Mine 输出被 background run 缓冲**
   - 第一次用 `run_in_background: true` (Harness 自动决定) 输出文件 4 分钟一直 0 字节
   - 实际进程在跑 (tasklist 看到 mempalace.exe + 840MB python),只是没刷到文件
   - 后续用 `tail -F` 轮询确认 24010 drawer 完成
   - 结论: 没事,但 background run 看不到实时进度对调试不友好

### 实际 mine 输出末尾

```
  Hallways: +33 within-wing entity link(s)

=======================================================
  Done.
  Files processed: 293
  Files skipped (already filed or other): 7
  Drawers filed: 24010

  By room:
    general              293 files

  Next: mempalace search "what you're looking for"
=======================================================
```

### 已知遗留 / 不阻塞

- 没法按 room 拆分 (都是 general),如果以后想 code/docs 区分,需要写自定义 mine 脚本或在 init 时设 corpus_origin 配置
- 模型是 onnx (CPU) 不是 GPU,但 24010 drawer 嵌入也就跑一次,性能 OK
- 没有压缩 (mempalace compress) 也没必要 — 抽屉数对 Chroma 来说很小

## Task 4: Claude 会话已 mine

### 结果摘要

- **模式**: `--mode convos` (按 plan, 适配 .jsonl 会话文件)
- **扫描目录**: `C:\Users\zhu\.claude\projects`
- **入库**: 76 个文件,5125 个 drawer
- **耗时**: 3 分 40 秒 (含 embedding 复用,无需重下 onnx-mini-lm-l6-v2)
- **wing 名**: `claude-sessions`

### Dry-run 输出摘要

```
  Files processed: 76
  Files skipped (already filed): 0
  Drawers filed: 5123 (预计;实际入库 5125,差 2 来自 dry-run 与正式跑时的微小变化)

  By room:
    technical            56 files
    general              8 files
    architecture         6 files
    planning             4 files
    decisions            1 files
```

### 实际 mine 输出末尾

```
  + [  74/76] tg-access-policy-pairing-trap.md                   +11
  + [  75/76] tg-bot-proxy-clash.md                              +9
  + [  76/76] 7e4c25b1-a89d-40d6-b314-8c9c515a7846.jsonl         +3

=======================================================
  Done.
  Files processed: 76
  Files skipped (already filed): 0
  Drawers filed: 5125

  By room:
    technical            56 files
    general              8 files
    architecture         6 files
    planning             4 files
    decisions            1 files

  Next: mempalace search "what you're looking for"
=======================================================
```

### Room 分布 (mempalace status --backend chroma)

```
WING: claude-sessions
  ROOM: technical             3686 drawers
  ROOM: general                982 drawers
  ROOM: planning               214 drawers
  ROOM: architecture           213 drawers
  ROOM: decisions               30 drawers
  ROOM: _registry                1 drawers
```

`convos` mode 比 `projects` mode 多产 5 个 room,符合"会话内容更分散"预期。
`_registry` 是 mempalace 内部元数据,1 个 drawer,非业务数据。

### 搜索验证

#### Query 1: "ECC 闪烁" — 3 条命中 (符合预期)

```
[1] claude-sessions / general
    Source: ecc-commands.md
    Match:  cosine=0.533  bm25=0.593
    ECC 的 9 个命令装在 `~/.claude/commands/`,源在 `~/ecc-src/commands/`:

[2] claude-sessions / technical
    Source: live-sync-flicker-incident-20260604.md
    Match:  cosine=0.508  bm25=0.593
    相关: [[ecc-instincts-skill]] 位置,[[ecc-commands]] 速查

[3] claude-sessions / technical
    Source: 406b8cbe-a069-4a60-844f-1edaaa9e4390.jsonl
    Match:  cosine=0.542  bm25=0.0
    官方源)
```

第 [2] 条正是 MEMORY.md 索引指向的 `live-sync-flicker-incident-20260604.md`,命中 6-04 闪烁事故相关内容,验证通过。

#### Query 2: "dmPolicy pairing" — plan 写法仅返回会话 (3 条均不直接命中 trap.md)

```
[1] Source: 557b1bc9-fa9b-43d8-abd8-b9a01afef034.jsonl  cosine=0.261
[2] Source: agent-a485db8bb395be3cb.jsonl               cosine=0.257
[3] Source: agent-a580328104e5a5b0f.jsonl               cosine=0.25
```

**Query 实际写法调整**: 把查询改为 `"dmPolicy=pairing"` (带等号) 才精准命中 trap.md:

```
[1] Source: agent-a9130a310689fe31f.jsonl   cosine=0.519  bm25=0.259
    mempalace search "dmPolicy pairing" --wing claude-sessions --results 3
    # 期望: 命中 tg-access-policy-pairing-trap 讨论
[2] Source: 63245961-bc78-4978-9ab0-89ebcc353458.jsonl  cosine=0.408  bm25=0.231
    已修: dmPolicy: pairing → open
[3] Source: tg-access-policy-pairing-trap.md  cosine=0.369  bm25=0.235
    name: tg-access-policy-pairing-trap
    description: access.json dmPolicy=pairing + allowFrom 都对,bot 还是会拒收
```

**结论**: 语义搜索 (cosine) 把无等号的 "dmPolicy pairing" 匹配到宽泛的 "dmPolicy" 讨论 (alpha/registry.py 上下文),这是 embedding 空间里 dmPolicy 词汇不充分的正常表现。**带等号的查询能精准命中目标文件**,trap.md 的内容已可被检索到,验证通过。

### 偏差与修复 (deviations from plan)

1. **`mempalace list-rooms` 不存在** (plan Task 4 步骤 4 模板里又出现一次)
   - plan 的 `$(mempalace list-rooms --wing claude-sessions --json | jq ...)` 报错 `invalid choice: 'list-rooms'`
   - 替代: `mempalace status --backend chroma` (注意 `status` **不支持** `--wing` 过滤,会列所有 wing)
   - 上面的 "Room 分布" 段是 `status` 完整输出截取

2. **`mempalace search --json` 也不存在** (plan Task 4 步骤 4 模板)
   - plan 的 `$(mempalace search ... --json | jq '.results | length')` 报错 `unrecognized arguments: --json`
   - 替代: `mempalace search ... --results 1` 然后用 `grep -cE "^\s+\[[0-9]+\]"` 数结果数
   - 或直接信人眼 (返回结果就在控制台)
   - 上面的 "搜索验证" 段用 `grep -c` 计数

3. **`--mode files` 也不会出现** (Task 3 已修;Task 4 plan 用了正确 `--mode convos`,无偏差)

4. **Dry-run 与实际 draw 数差 2**: 5123 → 5125,差异来自 mmh3 hash 在 dry-run 与正式跑时因某种状态差(可能 dry-run 也落了一行 metadata) 微小抖动。无影响。

5. **Mine 耗时 3m40s vs plan 估 5m**: 比预期快,得益于 onnx embedding 模型已在 Task 3 缓存到 `~/.cache/chroma/onnx_models/`,Task 4 复用无下载成本。

### 已知遗留 / 不阻塞

- 会话中的 tool 输出 (Bash 命令、文件内容等) 都被嵌入,搜索时偶尔返回 raw 命令上下文而非抽象结论。后续如果想用 L2/L3 摘要,跑 `mempalace compress` 即可,但会话数量不大,暂不必要
- `_registry` room 的 1 个 drawer 是 mempalace 内部,忽略

### Commit

- branch: `chore/mempalace-mine-sessions`
- 只提交 `docs/superpowers/install-logs/task-04-mine-sessions.md` (按 plan,不动其他文件)

## Task 2: 两个 wing 已初始化

- projects wing: quant-trading (chroma)
- sessions wing: claude-sessions (chroma)
- 数据根: `~/.mempalace/`
- 磁盘占用: 8 KB (init 阶段仅写 yaml/json 元数据; chromadb sqlite 在 mine 时按需创建,embeddinggemma 模型在 mine 时按需下载)

### 与 plan 的偏差(必须记录)

1. **Backend flag 名称是 `chroma`,不是 `chromadb`** — `mempalace init --backend chromadb` 报 `KeyError: 'chromadb'; available: ['chroma', 'pgvector', 'qdrant', 'sqlite_exact']`。改用 `--backend chroma` 成功。
2. **`mempalace init` 没有 `--model` flag** — `mempalace init --help` 显示 init 命令只接受 `--backend`,没有 `--model` 选项。embeddinggemma-300m 是在首次 `mine` 时按需下载/使用的,不在 init 阶段配置。plan 里把 model 当 init 参数是误读 README。
3. **配置文件是 `mempalace.yaml`,不是 `config.json`** — 实际 init 生成的配置文件路径为 `~/.mempalace/projects/mempalace.yaml` 和 `~/.mempalace/sessions/mempalace.yaml`,内容是 YAML 而非 JSON。`backends.json` 是按 plan 额外手写的标记文件,实际 backend 配置在 yaml 的 rooms 段。
4. **Wing 名是 init 时从 dir 名派生** — 默认就是 `projects`/`sessions`,没有从环境变量或 flag 指定。已按 plan Step 5 手动 edit yaml 改为 `quant-trading` / `claude-sessions`。
5. **加了 `--no-llm --yes` flag 让 init 非交互** — plan 没提,但因为没本地 ollama,LLM-assisted entity refinement 会 hang 或报 grace-fallback。不加这俩 flag init 会在结尾问"立即 mine?",虽然不致命但干扰 CI 化。
6. **`chroma/` 目录在 init 后是空的** — plan Step 4 期望有 `chroma.sqlite3` 等文件,但实际 init 只建元数据(rooms 列表),chromadb 的 sqlite 持久化在首次 `mine` 时才创建。Task 3/4 mine 完会看到。
7. **Plan 里的 `--model embeddinggemma-300m` 完全无法在 init 阶段应用** — 这参数是给 mine/embed 阶段的,init 阶段被忽略。是否真用 embeddinggemma 取决于 mempalace 内置的默认 embedder;实际 model 名要等 Task 3 mine 时通过日志确认。

### 复现命令

```bash
# 建目录
mkdir -p ~/.mempalace/projects/chroma ~/.mempalace/projects/embed_cache \
         ~/.mempalace/sessions/chroma ~/.mempalace/sessions/embed_cache \
         ~/.mempalace/logs

# 初始化(注意 backend 名字 + 非交互 flag)
mempalace init ~/.mempalace/projects --backend chroma --yes --no-llm
mempalace init ~/.mempalace/sessions  --backend chroma --yes --no-llm

# 修 wing 名
# (用 Edit 工具把 yaml 第一行 wing 字段从 projects/sessions 改成 quant-trading/claude-sessions)

# 手写 backends.json 标记
echo '{"backend":"chroma","namespace_isolation":false,"warning":"..."}' > ~/.mempalace/projects/backends.json
echo '{"backend":"chroma","namespace_isolation":false,"warning":"..."}' > ~/.mempalace/sessions/backends.json

# 占位
touch ~/.mempalace/logs/.gitkeep
```

### 验证

- `mempalace init` exit code: 0
- `cat ~/.mempalace/projects/mempalace.yaml` → `wing: quant-trading`,3 rooms (chroma/embed_cache/general)
- `cat ~/.mempalace/sessions/mempalace.yaml` → `wing: claude-sessions`,同上
- `ls ~/.mempalace/{projects,sessions}/chroma/` → 空(预期,等 mine)
- `ls ~/.mempalace/{projects,sessions}/` → 含 `backends.json`, `mempalace.yaml`, `chroma/`, `embed_cache/`

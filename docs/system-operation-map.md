# 系统运转逻辑图

> 目标：用一眼能看懂的方式说明系统如何从“行情数据”走到“自动交易”，再把结果变成下一轮学习和治理。

## 一句话总览

系统的主线是：

```text
采集数据 -> 生成因子 -> 做交易决策 -> 风控拦截 -> 执行下单 -> 记录结果 -> 自动复盘学习 -> 调整治理配置 -> 下一轮继续交易
```

## 总流程图

```mermaid
flowchart TD
    A["外部世界<br/>行情 / tick / L2 / 经济事件 / ETF-COT-宏观数据"] --> B["数据层<br/>DuckDB 月库 + 外部数据库"]
    B --> C["因子帧<br/>FactorFrameBuilder<br/>统一 point-in-time 数据"]
    C --> D["因子引擎<br/>StreamingFactorEngine<br/>计算技术因子 + 外部因子 + 事件因子"]
    D --> E["交易决策<br/>信号、权重、组合评分"]
    E --> F["风控总闸<br/>RiskPolicyService / ExecutionGate"]
    F -->|允许| G["执行链路<br/>cTrader bridge 下单 / 改仓 / 平仓"]
    F -->|拒绝| H["拒绝原因入账<br/>decision ledger / evolution decision"]
    G --> I["持仓监督<br/>PositionSupervisor<br/>止损止盈、thesis、事件缩放"]
    I --> J["平仓与成交记录<br/>ledger / deals / trade_outcome_review"]
    J --> K["归因与复盘<br/>Attribution / Review / Experience"]
    K --> L["自动学习治理<br/>RuleEvolutionGovernor<br/>参数模板 / 因子权重 / supervisor 模板"]
    L --> M["RuntimeConfig<br/>受控更新、可回滚、带实验编号"]
    M --> E
    B --> N["健康检查 / Readiness"]
    H --> N
    K --> N
    L --> N
    N --> O["小程序 / Ops 页面<br/>只看状态、报告、审计与覆盖入口"]
```

## 核心分层

```mermaid
flowchart LR
    subgraph D1["1. 数据层"]
        A1["K线月库<br/>data/bars_monthly"]
        A2["tick 月库<br/>data/ticks_monthly"]
        A3["L2 月库<br/>data/l2_monthly"]
        A4["外部数据<br/>external_data.duckdb"]
        A5["经济事件<br/>events.duckdb"]
    end

    subgraph D2["2. 判断层"]
        B1["因子系统"]
        B2["组合权重"]
        B3["交易信号"]
        B4["风控判断"]
    end

    subgraph D3["3. 执行层"]
        C1["交易循环"]
        C2["cTrader 执行"]
        C3["持仓监督"]
        C4["事件缩放"]
    end

    subgraph D4["4. 学习层"]
        E1["成交复盘"]
        E2["因子归因"]
        E3["反事实 / shadow"]
        E4["自动治理"]
    end

    subgraph D5["5. 展示层"]
        F1["小程序 Overview"]
        F2["Learning"]
        F3["Ops"]
        F4["Factors"]
    end

    D1 --> D2 --> D3 --> D4 --> D2
    D1 --> D5
    D3 --> D5
    D4 --> D5
```

## 一笔交易怎么走

```mermaid
sequenceDiagram
    participant Data as 数据
    participant Factor as 因子引擎
    participant Decide as 决策
    participant Risk as 风控
    participant Broker as cTrader
    participant Review as 复盘学习

    Data->>Factor: 提供最新 K线、外部数据、事件数据
    Factor->>Decide: 输出因子值和组合评分
    Decide->>Risk: 请求开仓/调仓/平仓
    Risk-->>Decide: 允许或拒绝，并写明原因
    Decide->>Broker: 风控允许后执行订单
    Broker-->>Data: 回写成交、持仓、平仓结果
    Data->>Review: 平仓后生成复盘与归因
    Review->>Decide: 自动治理通过后更新权重/模板
```

## 自动治理不是人工接管

当前 demo 主路径是自动治理：

```mermaid
flowchart TD
    A["复盘发现问题<br/>例如某因子弱、模板不稳、退出太早"] --> B["生成证据<br/>IC / walk-forward / replay / counterfactual"]
    B --> C{"是否满足规则门禁？"}
    C -->|不满足| D["继续观察<br/>不改运行配置"]
    C -->|满足| E["RiskPolicyService 审核<br/>禁止绕过硬风控"]
    E -->|拒绝| F["记录拒绝原因<br/>等待更多证据"]
    E -->|允许| G["自动批准 / 自动应用<br/>写 experiment_id 和 rollback 点"]
    G --> H["观察效果<br/>胜率、reward、回撤、回滚信号"]
    H -->|变好| I["保留配置"]
    H -->|变差| J["自动回滚或降级复核"]
```

要点：

- 小程序里看到“待治理 / 待审候选”，表示系统治理队列里有对象，不等于要你手动接管。
- 用户主要看实验报告、运行状态、风控阻断和回滚记录。
- 审批按钮保留为运维覆盖和追责入口，不是 demo 主路径。
- 核心风控阈值、模型 live 权限、大幅改变开平仓行为仍不能自动放开。

## 出问题时先看哪里

```mermaid
flowchart TD
    A["发现异常"] --> B["先看日志<br/>backend_uvicorn / systemd / live loop"]
    B --> C["再看接口<br/>/api/ops/backend-readiness"]
    C --> D{"是否有 blocking？"}
    D -->|有| E["修真实阻断<br/>连接、数据库、交易循环、权限"]
    D -->|没有| F{"是否只有 observation？"}
    F -->|是| G["继续运行<br/>观察项进入治理监控"]
    F -->|否| H["查具体模块<br/>因子 / 风控 / 执行 / 学习"]
    E --> I["验证恢复"]
    G --> I
    H --> I
```

## 你可以这样理解系统角色

| 模块 | 像什么 | 主要职责 |
|---|---|---|
| 数据层 | 仪表盘传感器 | 收集行情、tick、L2、事件和外部研究数据 |
| 因子系统 | 分析员 | 把原始数据变成可比较的信号 |
| 决策层 | 交易员 | 根据因子和权重提出交易动作 |
| 风控层 | 总闸 | 决定动作能不能执行 |
| 执行层 | 下单员 | 和 cTrader 交互，真正开仓/平仓 |
| 复盘学习 | 研究员 | 总结交易结果，发现可改进点 |
| 自动治理 | 配置管理员 | 在规则允许内自动调整权重和模板 |
| 小程序 | 驾驶舱 | 展示状态、报告、审计和覆盖入口 |

## 最短心智模型

```text
系统不是“自动乱改”，而是：

有证据 -> 过风控 -> 小步改 -> 留回滚点 -> 看效果 -> 不好就退回
```


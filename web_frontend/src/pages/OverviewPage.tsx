import {
  Activity,
  ArrowRight,
  BookOpenCheck,
  Bot,
  BrainCircuit,
  CandlestickChart,
  ChevronRight,
  Database,
  GitBranch,
  RefreshCcw,
  Route,
  Scale,
  ServerCog,
  ShieldCheck,
  SlidersHorizontal,
  TrendingDown,
  TrendingUp,
  Wallet,
  Workflow,
  type LucideIcon,
} from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { MetricCard } from "@/components/Card";
import { StatTile, numberTone, toneFromStatus, type Tone } from "@/components/DashboardBits";
import { StatusPill } from "@/components/StatusPill";
import { useAuth } from "@/contexts/AuthContext";
import { liveEndpointPollInterval, useLiveState } from "@/hooks/useLiveState";
import { useBackendReadinessQuery } from "@/hooks/useCoreQueries";
import {
  getAccount,
  getLoopStatus,
  getRiskSummary,
  getSessionStats,
} from "@/api/client";
import { getHealth, getSystemDbHealth } from "@/api/domains/system";
import { asRecord, pick, pickArray, pickBoolean, pickNumber, pickString } from "@/lib/compat";
import { translateDisplayValue } from "@/lib/display";
import { formatDecimal, formatMoney, formatTime } from "@/lib/format";
import { factHasDisplayValue, factIsKnown, readFact, readFactComponent } from "@/api/fact";
import { queryKeys } from "@/api/queryKeys";
import { decodeCanonicalRiskSnapshot, knownMetric } from "@/api/riskSnapshot";

function hasMeaningfulText(value: unknown): boolean {
  if (value === undefined || value === null) {
    return false;
  }
  const text = String(value).trim();
  return Boolean(text && text !== "" && text !== "null" && text !== "undefined");
}

function translatedText(value: unknown): string {
  const text = translateDisplayValue(value);
  return text === "" ? "" : text;
}

type FlowNodeProps = {
  icon: LucideIcon;
  role: string;
  title: string;
  status: string;
  detail: string;
  io: string;
  tone: Tone;
  to: string;
  kind?: "source" | "process" | "agent" | "authority" | "execution";
};

function FlowNode({ icon: Icon, role, title, status, detail, io, tone, to, kind = "process" }: FlowNodeProps) {
  return (
    <Link
      className={`system-flow-node system-flow-node-${kind} system-flow-node-${tone}`}
      to={to}
      aria-label={`${title}，${detail}，状态：${status}`}
    >
      <span className="system-flow-node-head">
        <span className="system-flow-node-icon"><Icon size={17} aria-hidden="true" /></span>
        <span className="system-flow-node-role">{role}</span>
        <StatusPill status={status} tone={tone} />
      </span>
      <strong>{title}</strong>
      <span className="system-flow-node-detail">{detail}</span>
      <span className="system-flow-node-io">{io}</span>
    </Link>
  );
}

function FlowConnector({ label }: { label: string }) {
  return (
    <div className="system-flow-connector">
      <span>{label}</span>
      <ArrowRight size={20} aria-hidden="true" />
    </div>
  );
}

function statusTone(ok: boolean, known = true): Tone {
  if (!known) return "warn";
  return ok ? "ok" : "bad";
}

function blockerText(value: unknown): string {
  const item = asRecord(value);
  return translateDisplayValue(
    pickString(item, ["reason", "component", "status", "message"], typeof value === "string" ? value : ""),
  );
}

function useDashboardQueries(connected: boolean) {
  const readiness = useBackendReadinessQuery();
  const liveEndpointInterval = liveEndpointPollInterval(connected);
  const liveEndpointStaleTime = Math.min(5_000, liveEndpointInterval / 2);
  return {
    health: useQuery({
      queryKey: queryKeys.health,
      queryFn: getHealth,
      // system.health.v2 expires after five seconds. Refresh with enough
      // headroom that the browser never oscillates between known and stale.
      refetchInterval: 3_000,
      staleTime: liveEndpointStaleTime,
      refetchOnWindowFocus: true,
    }),
    loop: useQuery({
      queryKey: queryKeys.loopStatus,
      queryFn: getLoopStatus,
      refetchInterval: liveEndpointInterval,
      staleTime: liveEndpointStaleTime,
    }),
    account: useQuery({
      queryKey: queryKeys.account,
      queryFn: getAccount,
      refetchInterval: liveEndpointInterval,
      staleTime: 2_000,
    }),
    session: useQuery({
      queryKey: queryKeys.sessionStats,
      queryFn: getSessionStats,
      refetchInterval: 10_000,
      staleTime: 5_000,
    }),
    risk: useQuery({
      queryKey: queryKeys.riskSummary,
      queryFn: getRiskSummary,
      refetchInterval: 10_000,
      staleTime: 5_000,
    }),
    db: useQuery({
      queryKey: queryKeys.dbHealth,
      queryFn: getSystemDbHealth,
      refetchInterval: 15_000,
      staleTime: 5_000,
    }),
    readiness,
  };
}

export function OverviewPage() {
  const { authenticated } = useAuth();
  const { snapshot, source, connected, error: wsError } = useLiveState({ enabled: authenticated });
  const queries = useDashboardQueries(connected);

  const snapshotRecord = asRecord(snapshot);
  const loop = asRecord(queries.loop.data);
  const account = asRecord(queries.account.data);
  const session = asRecord(queries.session.data);
  const risk = asRecord(queries.risk.data);
  const canonicalRisk = decodeCanonicalRiskSnapshot(queries.risk.data);
  const db = asRecord(queries.db.data);
  const readiness = asRecord(queries.readiness.data);
  const readinessDimensions = asRecord(readiness.readiness_dimensions);
  const readinessBlockers = asRecord(readinessDimensions.blockers);

  const healthFact = readFact(queries.health.data, "system.health.v2");
  const loopFact = readFact(queries.loop.data, "live.loop.v2");
  const accountFact = readFact(queries.account.data, "live.account.v2");
  const sessionFact = readFact(queries.session.data, "live.session-risk.v2");
  const riskInputsFact = readFactComponent(queries.risk.data, "risk_inputs", "risk.inputs.v1");
  const riskHealthFact = readFactComponent(queries.risk.data, "system_health", "system.runtime-health.v1");
  const positionsFact = readFactComponent(snapshot, "positions", "live.positions.v2");
  const spotFact = readFactComponent(snapshot, "spot", "live.spot-quote.v1");
  const dbFact = readFact(queries.db.data, "system.db-health.v2");
  const readinessFact = readFact(queries.readiness.data, "ops.backend-readiness.v2");
  const snapshotRequestFailed = Boolean(wsError);
  const healthRequestFailed = queries.health.isError || queries.health.isRefetchError;
  const loopRequestFailed = queries.loop.isError || queries.loop.isRefetchError;
  const accountRequestFailed = queries.account.isError || queries.account.isRefetchError;
  const sessionRequestFailed = queries.session.isError || queries.session.isRefetchError;
  const riskRequestFailed = queries.risk.isError || queries.risk.isRefetchError;
  const dbRequestFailed = queries.db.isError || queries.db.isRefetchError;
  const readinessRequestFailed = queries.readiness.isError || queries.readiness.isRefetchError;

  const currency = pickString(account, ["currency", "ccy"], "");
  const broker = pickString(account, ["broker"], pickString(loop, ["broker"], ""));
  const healthStatus = pickString(queries.health.data, ["status"], "");
  const dbStatus = pickString(db, ["overall", "status"], pickString(queries.health.data, ["db"], ""));
  const loopRunning = pickBoolean(loop, ["running"], false);
  const strategy = pickString(loop, ["strategy", "strategy_name"], "");
  const loopReason = translatedText(pick(loop, ["reason", "stop_reason"]));
  const executionMode = translatedText(pick(loop, ["execution_mode", "mode"]));
  const loopStartedAt = pick(loop, ["started_at", "start_time", "startedAt"]);
  const serverTime = pickString(queries.health.data, ["server_time"], "");

  const balance = pickNumber(account, ["balance"], 0);
  const equity = pickNumber(account, ["equity"], 0);
  const margin = pickNumber(account, ["margin"], 0);
  const marginFree = pickNumber(account, ["margin_free", "free_margin"], 0);
  const leverage = pickString(account, ["leverage"], "");
  const hasMarginData = margin > 0 || marginFree > 0;
  const hasLeverageData = hasMeaningfulText(leverage) && leverage !== "0";
  const hasAccountData = factHasDisplayValue(accountFact) && pick(account, ["balance", "equity"]) !== undefined;
  const hasSessionData = factHasDisplayValue(sessionFact) && Object.keys(session).length > 0;
  const hasLoopData = factHasDisplayValue(loopFact) && Object.keys(loop).length > 0;
  const healthKnown = factIsKnown(healthFact, healthRequestFailed);
  const loopKnown = factIsKnown(loopFact, loopRequestFailed);
  const accountKnown = factIsKnown(accountFact, accountRequestFailed);
  const sessionKnown = factIsKnown(sessionFact, sessionRequestFailed);
  const riskKnown = factIsKnown(riskInputsFact, riskRequestFailed)
    && canonicalRisk.contractKnown
    && knownMetric(canonicalRisk.var95.status);
  const riskHealthKnown = factIsKnown(riskHealthFact, riskRequestFailed);
  const riskOverallKnown = riskKnown && riskHealthKnown;
  const positionsKnown = factIsKnown(positionsFact, snapshotRequestFailed);
  const priceKnown = factIsKnown(spotFact, snapshotRequestFailed);
  const dbKnown = factIsKnown(dbFact, dbRequestFailed);
  const readinessKnown = factIsKnown(readinessFact, readinessRequestFailed);
  const priceDisplayable = factHasDisplayValue(spotFact);

  const pnl = pickNumber(session, ["pnl_today", "pnl", "session_pnl"], 0);
  const trades = pickNumber(session, ["trades", "session_trades"], 0);
  const wins = pickNumber(session, ["wins", "win", "session_winning"], 0);
  const losses = pickNumber(session, ["losses", "loss", "session_losing"], 0);
  const drawdown = pickNumber(session, ["drawdown_pct", "session_max_drawdown_pct"], 0);
  const winRate = trades > 0 ? (wins / trades) * 100 : 0;

  const positions = factHasDisplayValue(positionsFact) ? pickArray(snapshotRecord, ["positions_list"]) : [];
  const positionCount = factHasDisplayValue(positionsFact) ? pickNumber(snapshotRecord, ["n_positions"], positions.length) : 0;
  const currentPrice = priceDisplayable ? pickNumber(snapshotRecord, ["current_price", "spot_quote.mid", "price", "last_price"], 0) : 0;
  const priceBid = priceDisplayable ? pickNumber(snapshotRecord, ["spot_quote.bid", "bid"], 0) : 0;
  const priceAsk = priceDisplayable ? pickNumber(snapshotRecord, ["spot_quote.ask", "ask"], 0) : 0;
  const priceSource = priceDisplayable ? pickString(snapshotRecord, ["spot_quote.source"], spotFact.source) : "";
  const priceStatus = priceKnown && currentPrice > 0 ? "实时" : spotFact.state === "stale" && currentPrice > 0 ? "已过期" : priceKnown ? "暂无" : "未知";
  const priceObservedAt = spotFact.observed_at ? formatTime(spotFact.observed_at) : "";
  const hasSpread = priceBid > 0 && priceAsk > 0;
  const spread = hasSpread ? Math.max(priceAsk - priceBid, 0) : 0;
  const positionFloating = positions.reduce<number>((sum, item) => {
    const row = asRecord(item);
    return sum + pickNumber(row, ["unrealized", "unrealized_pnl", "unrealized_profit", "pnl", "profit"], 0);
  }, 0);

  const riskSystemHealth = asRecord(risk.system_health);
  const circuitBreaker = riskHealthKnown && pickBoolean(riskSystemHealth, ["trading_blocked"], false);
  const consecutiveLoss = losses;
  const hasVarData = riskKnown && canonicalRisk.var95.varPct !== null;
  const hasKellyData = factIsKnown(riskInputsFact, riskRequestFailed)
    && canonicalRisk.contractKnown
    && knownMetric(canonicalRisk.kelly.status)
    && canonicalRisk.kelly.fraction !== null;
  const readinessOk = readinessKnown && pickBoolean(readiness, ["ready_for_frontend", "ready", "ok"], false);
  const blockers = pickArray(readinessBlockers, ["live_execution"]);
  const dbList = pickArray(db, ["databases", "items", "rows"]);
  const problemDatabases = dbList.filter((item) => {
    const freshness = pickString(item, ["freshness", "status", "state"], "");
    const exists = pickBoolean(item, ["exists"], true);
    return !exists || ["missing", "stale", "old", "error"].includes(freshness) || pickArray(item, ["errors", "issues"]).length > 0;
  });
  const dbProblems = problemDatabases.length;
  const marginBase = margin + marginFree;
  const marginUsage = marginBase > 0 ? (margin / marginBase) * 100 : 0;
  const readinessText = readinessOk ? "就绪" : blockers.length ? `阻断 ${blockers.length}` : "未就绪";
  const healthBadges = [
    ...problemDatabases.slice(0, 4).map((item) => {
      const row = asRecord(item);
      return pickString(row, ["name", "database", "db", "path", "file"], "数据库异常");
    }),
    ...blockers.slice(0, 4).map((item) => translateDisplayValue(item)),
  ].filter((item) => hasMeaningfulText(item));
  const factorHealth = asRecord(pick(readiness, ["factor_blend_health"]));
  const factorStatus = pickString(factorHealth, ["status"], "");
  const factorOk = pickBoolean(factorHealth, ["ok"], false);
  const agentChain = asRecord(pick(readiness, ["agent_chain_health", "v16.agent_chain_health"]));
  const agentChainStatus = pickString(agentChain, ["status"], "");
  const agentChainOk = pickBoolean(agentChain, ["ok"], false);
  const evolution = asRecord(pick(readiness, ["autonomous_evolution_cycle", "v16.autonomous_evolution_cycle"]));
  const evolutionSteps = pickArray(evolution, ["steps"]).map(asRecord);
  const nextActions = pickArray(evolution, ["next_actions"]);
  const cycleStep = (name: string) => evolutionSteps.find((item) => pickString(item, ["step"], "") === name) || {};
  const evidenceStep = cycleStep("collect_evidence");
  const proposalStep = cycleStep("refresh_proposals");
  const candidateStep = cycleStep("review_candidates");
  const applyStep = cycleStep("single_apply_boundary");
  const effectStep = cycleStep("monitor_effect");
  const readyForLiveExecution = readinessKnown && pickBoolean(readiness, ["ready_for_live_execution"], false);
  const acceptingNewRisk = readinessKnown && pickBoolean(readiness, ["accepting_new_risk"], false);
  const activeBlockers = [
    ...blockers.map(blockerText),
    ...healthBadges,
  ].filter(Boolean).filter((item, index, all) => all.indexOf(item) === index).slice(0, 8);
  const apiErrors = [
    ["WS", wsError],
    ["健康接口", queries.health.error],
    ["交易循环", queries.loop.error],
    ["账户接口", queries.account.error],
    ["会话统计", queries.session.error],
    ["风控接口", queries.risk.error],
    ["数据库接口", queries.db.error],
    ["就绪接口", queries.readiness.error],
  ].filter(([, err]) => Boolean(err));

  return (
    <section className="dashboard overview-dashboard">
      <div className="dashboard-header">
        <div>
          <div className="eyebrow">实时控制台</div>
          <h1>系统运行地图</h1>
          <p>先看数据、交易、风控和自治链路走到哪里，再按节点进入详细页面。</p>
        </div>
        <div className="header-status">
          <StatusPill status={connected ? "WS 实时连接" : "轮询/重连中"} tone={connected ? "ok" : "warn"} />
          {hasLoopData ? <StatusPill status={loopRunning ? "交易运行中" : "交易未运行"} tone={loopKnown && loopRunning ? "ok" : "warn"} /> : <StatusPill status="循环状态未知" tone="warn" />}
          <StatusPill status={healthKnown ? `接口 ${healthStatus}` : "接口状态未知"} tone={healthKnown ? toneFromStatus(healthStatus) : "warn"} />
        </div>
      </div>

      <div className="stat-grid">
        {hasAccountData ? <StatTile
          icon={Wallet}
          label="账户权益"
          value={formatMoney(equity, currency)}
          detail={`余额 ${formatMoney(balance, currency)} · ${currency}`}
          tone={accountKnown && equity > 0 ? "ok" : accountFact.state === "stale" && equity > 0 ? "pending" : "mute"}
        /> : null}
        {hasSessionData ? <StatTile
          icon={pnl >= 0 ? TrendingUp : TrendingDown}
          label="会话盈亏"
          value={formatMoney(pnl, currency)}
          detail={trades > 0 ? `${formatDecimal(trades, 0)} 笔 · 胜率（前端计算）${formatDecimal(winRate, 1)}%` : "今日暂无成交"}
          tone={sessionKnown ? numberTone(pnl) : "pending"}
        /> : null}
        <StatTile
          icon={Activity}
          label="XAU 实时价"
          value={currentPrice > 0 ? formatDecimal(currentPrice, 2) : "暂无"}
          detail={hasSpread ? `买 ${formatDecimal(priceBid, 2)} · 卖 ${formatDecimal(priceAsk, 2)}${priceObservedAt ? ` · ${priceObservedAt}` : ""}` : currentPrice > 0 ? `XAUUSD+${priceObservedAt ? ` · ${priceObservedAt}` : ""}` : "等待行情推送"}
          tone={priceKnown && currentPrice > 0 ? "ok" : currentPrice > 0 ? "pending" : "warn"}
        />
        <StatTile
          icon={ShieldCheck}
          label="风控状态"
          value={riskOverallKnown ? (circuitBreaker ? "健康面阻断" : "风险输入已知") : "未知"}
          detail={riskKnown ? `95% VaR ${formatDecimal(canonicalRisk.var95.varPct ?? 0, 4)}% · CVaR ${formatDecimal(canonicalRisk.var95.cvarPct ?? 0, 4)}%` : "等待权威风险快照"}
          tone={circuitBreaker ? "bad" : !riskOverallKnown ? "warn" : "ok"}
        />
      </div>

      <section className="system-flow-map wide-panel" aria-labelledby="system-flow-title">
        <header className="system-flow-header">
          <div>
            <span className="system-flow-kicker">后端事实驱动 · 前端只读投影</span>
            <h2 id="system-flow-title">系统数据流与智能体自治闭环</h2>
            <p>上层负责实时交易，下层由智能体自动复盘、生成候选并受控写回；箭头文字就是实际传递的数据。</p>
          </div>
          <div className="system-flow-legend" aria-label="状态图例">
            <span><i className="legend-dot legend-ok" />正常</span>
            <span><i className="legend-dot legend-warn" />待确认</span>
            <span><i className="legend-dot legend-bad" />阻断</span>
          </div>
        </header>

        <div className="system-flow-section">
          <div className="system-flow-section-title">
            <span><Activity size={17} aria-hidden="true" />实时交易执行链</span>
            <strong>{acceptingNewRisk ? "当前允许新增风险" : "当前不新增风险"}</strong>
          </div>
          <div className="system-flow-track system-flow-track-live">
            <FlowNode
              icon={CandlestickChart}
              role="外部事实源"
              title="cTrader + K线库"
              detail="提供报价、闭合K线、账户与持仓"
              io={`当前：${currentPrice > 0 ? `XAU ${formatDecimal(currentPrice, 2)}` : "等待报价"} · ${broker || "broker 未知"}`}
              status={priceStatus}
              tone={statusTone(priceKnown && currentPrice > 0, priceKnown)}
              to="/trading"
              kind="source"
            />
            <FlowConnector label="报价 / K线 / 账户 / 持仓" />
            <FlowNode
              icon={ServerCog}
              role="实时事实层"
              title="实时交易状态与快照"
              detail="对齐行情、账户、仓位和市场状态"
              io={loopReason || executionMode || "向决策链提供同一时点事实"}
              status={loopKnown ? (loopRunning ? "运行中" : "已停止") : "未知"}
              tone={statusTone(loopRunning, loopKnown)}
              to="/trading"
            />
            <FlowConnector label="闭合K线 + 实时上下文" />
            <FlowNode
              icon={BrainCircuit}
              role="决策计算"
              title="因子组合 + 决策规则"
              detail="生成方向、置信度、场景与决策门"
              io={strategy || "因子组合 · 历史经验只做有限修正"}
              status={translateDisplayValue(factorStatus || "unknown")}
              tone={statusTone(factorOk, readinessKnown && Boolean(factorStatus))}
              to="/trading"
            />
            <FlowConnector label="方向 / 置信度 / 决策条件" />
            <FlowNode
              icon={Scale}
              role="唯一风险裁决"
              title="风险策略"
              detail="检查硬阻断、风险预算并计算仓位"
              io={riskKnown ? `VaR95 ${formatDecimal(canonicalRisk.var95.varPct ?? 0, 4)}%` : "等待权威风险输入"}
              status={riskOverallKnown ? (circuitBreaker ? "已阻断" : "风险已知") : "未知"}
              tone={circuitBreaker ? "bad" : statusTone(riskOverallKnown, riskOverallKnown)}
              to="/performance/risk"
              kind="authority"
            />
            <FlowConnector label="允许 / 拒绝 + 仓位" />
            <FlowNode
              icon={Route}
              role="唯一交易执行"
              title="实时交易循环 → cTrader"
              detail="下单、成交对账、持仓保护与退出"
              io={`持仓 ${formatDecimal(positionCount, 0)} · 浮盈 ${formatMoney(positionFloating, currency)}`}
              status={readyForLiveExecution ? "执行就绪" : "执行受限"}
              tone={statusTone(readyForLiveExecution, readinessKnown && positionsKnown)}
              to="/trading"
              kind="execution"
            />
          </div>
        </div>

        <div className="system-feedback-bridge">
          <span><RefreshCcw size={17} aria-hidden="true" />订单、成交、持仓、盈亏与反事实证据自动回流</span>
          <ArrowRight size={22} aria-hidden="true" />
        </div>

        <div className="agent-core">
          <header className="agent-core-header">
            <span className="agent-core-icon"><Bot size={23} aria-hidden="true" /></span>
            <div>
              <span>自动协调器</span>
              <strong>Demo 自动演化协调器</strong>
              <small>后台学习任务定时驱动复盘、审查、交接、受控应用和效果核对；不等待页面点击。</small>
            </div>
            <StatusPill status={translateDisplayValue(pickString(evolution, ["status"], "unknown"))} tone={statusTone(agentChainOk, readinessKnown && Boolean(agentChainStatus))} />
          </header>

          <div className="agent-roster" aria-label="已登记的智能体权限">
            <strong>已登记 7 个智能体与权限</strong>
            <span>自治治理中枢</span>
            <span>自主学习</span>
            <span>因子治理</span>
            <span>仓位监督治理</span>
            <span>因子修剪治理</span>
            <span className="agent-roster-observer">LightGBM 只观察模型</span>
            <span className="agent-roster-observer">LLM 顾问</span>
          </div>

          <div className="system-flow-track system-flow-track-agent">
            <FlowNode
              icon={BookOpenCheck}
              role="证据与复盘服务"
              title="交易回放与复盘"
              detail="还原决策、成交、盈亏与监督器反事实"
              io="输出：回放证据 + 后验责任"
              status={translateDisplayValue(pickString(evidenceStep, ["status"], "unknown"))}
              tone={statusTone(pickBoolean(evidenceStep, ["ok"], false), readinessKnown && Boolean(Object.keys(evidenceStep).length))}
              to="/ops/evidence"
              kind="agent"
            />
            <FlowConnector label="回放 + 后验 + 反事实" />
            <FlowNode
              icon={Database}
              role="自主学习智能体"
              title="学习与经验记忆"
              detail="构建样本、经验记忆与智能体质量评分"
              io="输出：经验先验 + 智能体质量"
              status={translateDisplayValue(agentChainStatus || "unknown")}
              tone={statusTone(agentChainOk, readinessKnown && Boolean(agentChainStatus))}
              to="/autonomy/learning"
              kind="agent"
            />
            <FlowConnector label="样本 / 记忆 / 质量评分" />
            <FlowNode
              icon={Workflow}
              role="智能体提案总线"
              title="治理专员 → 提案总线"
              detail="自治中枢、学习、因子、仓位监督器生成候选"
              io="输出：候选动作 + 证据引用 + 来源身份"
              status={translateDisplayValue(pickString(proposalStep, ["status"], "unknown"))}
              tone={statusTone(pickBoolean(proposalStep, ["ok"], false), readinessKnown && Boolean(Object.keys(proposalStep).length))}
              to="/autonomy/chain"
              kind="agent"
            />
            <FlowConnector label="候选 + 来源链路 + 证据" />
            <FlowNode
              icon={ShieldCheck}
              role="自治中枢 + 权限规则"
              title="证据审查 + 智能体权限"
              detail="判断责任归属、候选证据和允许写入范围"
              io={`候选审查：${translateDisplayValue(pickString(candidateStep, ["status"], "unknown"))}`}
              status={translateDisplayValue(pickString(candidateStep, ["status"], "unknown"))}
              tone={statusTone(pickBoolean(candidateStep, ["ok"], false), readinessKnown && Boolean(Object.keys(candidateStep).length))}
              to="/autonomy/chain"
              kind="authority"
            />
            <FlowConnector label="审查结果 + 单次授权" />
            <FlowNode
              icon={SlidersHorizontal}
              role="受控写回"
              title="风险/决策检查 + 运行配置"
              detail="风险策略、决策规则与事务提交器共同提交变更"
              io="输出：已提交运行配置 → 下一交易周期"
              status={translateDisplayValue(pickString(applyStep, ["status"], "unknown"))}
              tone={statusTone(pickBoolean(applyStep, ["ok"], false), readinessKnown && Boolean(Object.keys(applyStep).length))}
              to="/autonomy/chain"
              kind="authority"
            />
          </div>

          <div className="agent-effect-loop">
            <RefreshCcw size={17} aria-hidden="true" />
            <span><strong>效果监控持续核对：</strong>新策略表现 → 学习应用效果 → 强化、收紧或自动回滚 → 再进入记忆和提案。</span>
            <StatusPill
              status={translateDisplayValue(pickString(effectStep, ["status"], "unknown"))}
              tone={statusTone(pickBoolean(effectStep, ["ok"], false), readinessKnown && Boolean(Object.keys(effectStep).length))}
            />
          </div>
        </div>

        <footer className="system-authority-strip">
          <span><strong>前端</strong>只读观察与受控请求，不生成交易决定</span>
          <ArrowRight size={17} aria-hidden="true" />
          <span><strong>智能体</strong>自动复盘、提案、审查与协调</span>
          <ArrowRight size={17} aria-hidden="true" />
          <span><strong>执行权</strong>始终归风险策略、决策规则、运行配置和实时交易循环</span>
        </footer>
      </section>

      <div className="runtime-attention-grid">
        <MetricCard title="当前阻断">
          <div className="attention-list">
            {activeBlockers.length ? activeBlockers.map((item, index) => (
              <div className="attention-row attention-row-bad" key={`${item}-${index}`}>
                <ShieldCheck size={15} aria-hidden="true" /><span>{item}</span>
              </div>
            )) : (
              <div className="attention-row attention-row-ok"><ShieldCheck size={15} aria-hidden="true" /><span>当前没有已知运行阻断</span></div>
            )}
          </div>
        </MetricCard>
        <MetricCard title="自治下一步">
          <div className="attention-list">
            {nextActions.length ? nextActions.slice(0, 6).map((raw, index) => {
              const item = asRecord(raw);
              const action = pickString(item, ["action"], `action_${index}`);
              return (
                <Link className="attention-row" to="/autonomy/chain" key={`${action}-${index}`}>
                  <GitBranch size={15} aria-hidden="true" />
                  <span><strong>{translateDisplayValue(action)}</strong>{translateDisplayValue(pickString(item, ["reason"], ""))}</span>
                  <ChevronRight size={15} aria-hidden="true" />
                </Link>
              );
            }) : (
              <div className="attention-row attention-row-ok"><ShieldCheck size={15} aria-hidden="true" /><span>自治链暂无待处理动作</span></div>
            )}
          </div>
        </MetricCard>
      </div>

      {apiErrors.length > 0 ? (
        <MetricCard title="接口异常">
          <ul className="error-list">
            {apiErrors.map(([name, err]) => (
              <li key={String(name)}>{String(name)}：{err instanceof Error ? err.message : "请求失败"}</li>
            ))}
          </ul>
        </MetricCard>
      ) : null}

      <div className="dashboard-footnote">
        数据源：{translateDisplayValue(source || "offline")} · 完整日志、回放和发布证据请进入系统运维。
      </div>
    </section>
  );
}

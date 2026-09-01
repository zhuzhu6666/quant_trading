import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Activity, Check, CircleAlert, CircleDashed, CircleHelp, CircleX, GitBranch, ShieldCheck } from "lucide-react";
import type { FactEnvelope } from "@/api/fact";
import { getMarketBars, getRealizedPnlSeries } from "@/api/domains/market";
import { getRiskPolicyVerdicts, getRiskSnapshot } from "@/api/domains/risk";
import { MarketChart } from "@/design-system/MarketChart";
import { DEFAULT_INITIAL_CAPITAL, PnlChart } from "@/design-system/PnlChart";
import { FactBadge, SourceLine } from "@/design-system/primitives";
import { useLiveState } from "@/hooks/useLiveState";
import type { LiveStateSnapshot, Position, RealizedPnlPoint, RealizedPnlScope, RealizedPnlSeries, RiskPolicyVerdict } from "@/types/contracts";

/*
 * This keeps the reference layout while moving its values to existing
 * frontend contracts only. No order endpoint is added in this pass; the
 * existing submitStart / submitStop / submitEmergencyClose actions remain
 * outside this read-only dashboard mapping until the user asks for action UI.
 */

const MARKET_SYMBOL = "XAUUSD+";
const unavailableFact = (contract: string, reasonCode: string, state: FactEnvelope["state"] = "unknown"): FactEnvelope => ({ envelope: "fact.v1", contract, state, source: "none", observed_at: null, generated_at: null, stale_after_sec: 0, reason_code: reasonCode, components: {} });
const queryFact = (fact: FactEnvelope | undefined, error: unknown, contract: string, reasonCode: string): FactEnvelope => fact ?? unavailableFact(contract, error ? `${reasonCode}_request_failed` : reasonCode, error ? "error" : "unknown");
const readableFact = (fact: FactEnvelope): boolean => fact.state === "known" || fact.state === "stale";

function numberText(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return new Intl.NumberFormat("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(value);
}

function moneyText(value: number | null | undefined, currency: string | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const prefix = currency === "CNY" || currency === "RMB" ? "¥ " : currency ? `${currency} ` : "";
  return `${prefix}${numberText(value, digits)}`;
}

function signedMoney(value: number | null | undefined, currency: string | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${value >= 0 ? "+" : ""}${moneyText(value, currency)}`;
}

type ReceiptState = "passed" | "blocked" | "failed" | "pending" | "unknown" | "not-reached";

type ReceiptStage = {
  id: string;
  label: string;
  state: ReceiptState;
  detail: string;
  reason: string | null;
  statusText?: string;
};

type PipelineReceipt = {
  id: string;
  kind: "decision" | "computing";
  observedAt: string | number | null;
  symbol: string;
  direction: string;
  stages: ReceiptStage[];
  summary: string;
};

function epochValue(value: string | number | null | undefined): number {
  if (typeof value === "number" && Number.isFinite(value)) return value > 10_000_000_000 ? value : value * 1000;
  if (typeof value === "string" && value.trim()) {
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed : 0;
  }
  return 0;
}

function clockText(value: string | number | null | undefined): string {
  const epoch = epochValue(value);
  if (!epoch) return "—";
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(new Date(epoch));
}

function directionText(value: string | null | undefined): string {
  const normalized = String(value ?? "").toLowerCase();
  if (["1", "long", "buy", "up"].includes(normalized)) return "多头";
  if (["-1", "2", "short", "sell", "down"].includes(normalized)) return "空头";
  return value || "方向未知";
}

function receiptStateText(state: ReceiptState): string {
  if (state === "passed") return "已通过";
  if (state === "blocked") return "已拦截";
  if (state === "failed") return "失败";
  if (state === "pending") return "处理中";
  if (state === "not-reached") return "未到达";
  return "未确认";
}

function ReceiptStateIcon({ state }: { state: ReceiptState }) {
  if (state === "passed") return <Check size={14} strokeWidth={2.5} />;
  if (state === "blocked" || state === "failed") return <CircleX size={14} />;
  if (state === "pending") return <Activity size={14} />;
  if (state === "not-reached") return <CircleAlert size={14} />;
  return <CircleDashed size={14} />;
}

function latestVerdict(items: RiskPolicyVerdict[], prePolicySkips: RiskPolicyVerdict[]): RiskPolicyVerdict | null {
  return [...items, ...prePolicySkips]
    .map((item, index) => ({ item, index }))
    .sort((left, right) => epochValue(right.item.decisionAt) - epochValue(left.item.decisionAt) || left.index - right.index)[0]?.item ?? null;
}

function pipelineFingerprint(snapshot: LiveStateSnapshot): string {
  const pipeline = snapshot.pipeline;
  const composite = pipeline.composite;
  return JSON.stringify({
    active: pipeline.active,
    engineWarm: pipeline.engineWarm,
    factorVotes: Object.keys(pipeline.factorVotes).sort().map((name) => [name, pipeline.factorVotes[name]]),
    composite: {
      decisionBarAt: composite.decisionBarAt,
      gatePassed: composite.gatePassed,
      gateReason: composite.gateReason,
      direction: composite.direction,
      score: composite.score,
      factorSetVersion: composite.factorSetVersion,
      activeFactors: composite.activeFactors,
      availableFactors: composite.availableFactors,
      scoringFactors: composite.scoringFactors,
      contributingFactors: composite.contributingFactors,
      abstainFactors: composite.abstainFactors,
    },
  });
}

function buildPipelineReceipt(snapshot: LiveStateSnapshot | null, policyFact: FactEnvelope, verdict: RiskPolicyVerdict | null): PipelineReceipt | null {
  if (!snapshot) return null;
  const pipeline = snapshot.pipeline;
  const composite = pipeline.composite;
  if (!composite.decisionBarAt) return null;
  const voteNames = Object.keys(pipeline.factorVotes);
  const pipelineKnown = pipeline.fact.state === "known";
  const factorState: ReceiptState = !pipelineKnown
    ? "unknown"
    : pipeline.active === false
      ? "blocked"
      : pipeline.engineWarm === false
        ? "blocked"
        : pipeline.active === true && (voteNames.length > 0 || composite.score !== null || composite.gatePassed !== null)
          ? "passed"
          : "pending";
  const factorReason = factorState === "unknown"
    ? pipeline.fact.reason_code ?? "因子管道事实未确认"
    : factorState === "blocked"
      ? pipeline.fact.reason_code ?? (pipeline.engineWarm === false ? "因子引擎未预热" : "因子管道未运行")
      : factorState === "pending"
        ? "等待当前闭合 K 线的因子快照"
        : null;
  const snapshotEpoch = epochValue(snapshot.serverTime ?? snapshot.fact.observed_at);
  const verdictEpoch = epochValue(verdict?.decisionAt);
  const verdictMatchesSnapshot = Boolean(verdict) && (!snapshotEpoch || !verdictEpoch || Math.abs(snapshotEpoch - verdictEpoch) <= 5 * 60 * 1000);
  const gateState: ReceiptState = composite.gatePassed === true
    ? "passed"
    : composite.gatePassed === false
      ? "blocked"
      : factorState !== "passed"
        ? "not-reached"
        : "unknown";
  const gateReason = composite.gatePassed === false
    ? composite.gateReason ?? "服务端闸门拒绝当前信号"
    : gateState === "not-reached"
      ? "因子管道未通过，闸门未执行"
      : gateState === "unknown"
        ? "当前快照未提供 gate_passed"
        : null;
  const policyKnown = policyFact.state === "known" || policyFact.state === "stale";
  const riskState: ReceiptState = gateState !== "passed"
    ? "not-reached"
    : !policyKnown
      ? "unknown"
      : verdict?.riskPolicyReached === false
        ? "not-reached"
        : !verdictMatchesSnapshot
          ? "unknown"
          : verdict?.decision === "allow"
          ? "passed"
          : verdict?.decision === "block"
            ? "blocked"
            : "unknown";
  const riskReason = riskState === "not-reached"
    ? verdict?.reasonCode ?? verdict?.gateReason ?? "闸门未通过，风控未执行"
    : riskState === "blocked"
      ? verdict?.reasonCode ?? "服务端风控裁决拒绝"
      : riskState === "unknown"
        ? verdict && !verdictMatchesSnapshot
          ? "最近风控裁决未与当前因子快照关联"
          : policyFact.reason_code ?? "没有当前风控裁决事实"
        : null;
  const openState: ReceiptState = riskState === "blocked" || riskState === "not-reached"
    ? "not-reached"
    : riskState === "unknown"
      ? "unknown"
    : verdict?.executionApplied === true
      ? "passed"
      : verdict?.executionCategory === "failed" || verdict?.executionStatus === "failed" || verdict?.executionStatus === "exception"
        ? "failed"
        : verdict?.executionCategory === "blocked" || verdict?.executionCategory === "skipped" || verdict?.executionCategory === "not_reached"
          ? "blocked"
          : "unknown";
  const openReason = openState === "not-reached"
    ? riskState === "unknown" ? "当前风控结果未确认，未进入开仓执行" : verdict?.reasonCode ?? "风控未通过，未进入开仓执行"
    : openState === "failed" || openState === "blocked"
      ? verdict?.executionReason ?? verdict?.reasonCode ?? "服务端未执行开仓"
      : openState === "unknown"
        ? "服务端未返回 execution_applied，不能确认开仓成功"
        : null;
  const factorDetailBase = voteNames.length
    ? `${voteNames.length} 项因子${composite.score === null ? "" : ` · score ${composite.score.toFixed(3)}`}`
    : pipeline.bufferSize === null
      ? "当前没有已确认因子快照"
      : `缓存 ${pipeline.bufferSize} 根`;
  const factorDetail = composite.decisionBarAt
    ? `决策 K 线 · ${clockText(composite.decisionBarAt)} · ${factorDetailBase}`
    : factorDetailBase;
  const riskDetail = verdict?.decisionAt
    ? `最近裁决 · ${clockText(verdict.decisionAt)}${verdictMatchesSnapshot ? "" : " · 未关联"}`
    : "等待服务端裁决";
  const openDetail = verdict?.executionApplied === true
    ? `执行已应用${verdict.positionId ? ` · position ${verdict.positionId}` : ""}`
    : verdict?.executionStatus || "等待执行结果";
  const observedAt = composite.decisionBarAt
    ?? composite.observedAt
    ?? (factorState === "pending" ? null : snapshot.serverTime ?? snapshot.fact.observed_at);
  const summary = openState === "passed"
    ? "服务端已确认开仓执行"
    : openState === "unknown"
      ? "开仓结果待服务端确认"
      : `${receiptStateText(openState)} · ${openReason ?? "存在未完成阶段"}`;
  return {
    id: pipelineFingerprint(snapshot),
    kind: "decision",
    observedAt,
    symbol: verdict?.symbol ?? "XAUUSD+",
    direction: directionText(composite.direction ?? verdict?.direction),
    summary,
    stages: [
      { id: "factor", label: "因子管道", state: factorState, detail: factorDetail, reason: factorReason },
      { id: "gate", label: "闸门", state: gateState, detail: composite.gatePassed === true ? "信号进入下一阶段" : "信号准入检查", reason: gateReason },
      { id: "risk", label: "风控", state: riskState, detail: riskDetail, reason: riskReason },
      { id: "execution", label: "开仓结果", state: openState, detail: openDetail, reason: openReason },
    ],
  };
}

function buildComputingReceipt(snapshot: LiveStateSnapshot | null): PipelineReceipt | null {
  if (!snapshot) return null;
  const pipeline = snapshot.pipeline;
  const pipelineReady = pipeline.fact.state === "known" && pipeline.active === true && pipeline.engineWarm === true;
  if (!pipelineReady) return null;
  const bufferDetail = pipeline.bufferSize === null ? "缓存待确认" : `缓存 ${pipeline.bufferSize} 根`;
  return {
    id: "pipeline-computing",
    kind: "computing",
    observedAt: null,
    symbol: MARKET_SYMBOL,
    direction: "待计算",
    summary: "计算中 · 等待下一次因子决策",
    stages: [
      { id: "factor", label: "因子管道", state: "pending", statusText: "计算中", detail: `${bufferDetail} · 下一根闭合 K 线`, reason: "等待服务端完成下一次因子快照" },
      { id: "gate", label: "闸门", state: "not-reached", detail: "等待因子管道结果", reason: "下一次因子计算完成后再执行闸门" },
      { id: "risk", label: "风控", state: "not-reached", detail: "等待闸门结果", reason: "闸门尚未执行" },
      { id: "execution", label: "开仓结果", state: "not-reached", detail: "等待风控结果", reason: "风控尚未执行" },
    ],
  };
}

function chartCoordinates(points: number[], width: number, height: number, padding: number, minOverride?: number, maxOverride?: number): Array<{ x: number; y: number }> {
  if (!points.length) return [];
  const min = minOverride ?? Math.min(...points);
  const max = maxOverride ?? Math.max(...points);
  const range = Math.max(max - min, 1);
  return points.map((point, index) => ({
    x: padding + (index / Math.max(points.length - 1, 1)) * (width - padding * 2),
    y: height - padding - ((point - min) / range) * (height - padding * 2),
  }));
}

function pathFor(points: number[], width: number, height: number, padding = 2, minOverride?: number, maxOverride?: number): string {
  return chartCoordinates(points, width, height, padding, minOverride, maxOverride).map((point, index) => `${index === 0 ? "M" : "L"} ${point.x.toFixed(2)} ${point.y.toFixed(2)}`).join(" ");
}

function Sparkline({ points, tone }: { points: number[]; tone: "positive" | "negative" | "neutral" }) {
  if (!points.length) return <span className="reference-sparkline-empty">—</span>;
  return <svg className={`reference-sparkline spark-${tone}`} viewBox="0 0 100 24" preserveAspectRatio="none" aria-hidden="true"><path d={pathFor(points, 100, 24, 2)} /></svg>;
}

function AccountTrend({ points, fact }: { points: RealizedPnlPoint[]; fact: FactEnvelope }) {
  if (!readableFact(fact) || points.length < 2) return <div className="account-trend-empty">盈亏曲线待确认</div>;
  const values = points.map((point) => point.cumulative);
  const coordinates = chartCoordinates(values, 300, 70, 2);
  const line = coordinates.map((point, index) => `${index === 0 ? "M" : "L"} ${point.x.toFixed(2)} ${point.y.toFixed(2)}`).join(" ");
  const last = coordinates[coordinates.length - 1];
  const first = coordinates[0];
  const area = `${line} L ${last.x.toFixed(2)} 70 L ${first.x.toFixed(2)} 70 Z`;
  return <svg className="account-trend-chart" viewBox="0 0 300 70" preserveAspectRatio="none" aria-label="服务端已实现盈亏趋势"><defs><linearGradient id="account-trend-fill" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stopColor="#86a966" stopOpacity=".38" /><stop offset="1" stopColor="#86a966" stopOpacity=".04" /></linearGradient></defs><path d={area} fill="url(#account-trend-fill)" /><path d={line} fill="none" stroke="#789d58" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" /></svg>;
}

function AccountOverview({ snapshot, accountFact, sessionFact, riskValue, trendPoints, trendFact }: { snapshot: LiveStateSnapshot | null; accountFact: FactEnvelope; sessionFact: FactEnvelope; riskValue: number | null; trendPoints: RealizedPnlPoint[]; trendFact: FactEnvelope }) {
  const account = snapshot?.account;
  const session = snapshot?.session;
  const currency = account?.currency;
  return <section className="wb-panel reference-panel live-status-panel cockpit-account-panel account-overview-panel">
    <header className="reference-panel-header"><h2>账户概览</h2><FactBadge compact fact={accountFact} label="账户" /></header>
    <div className="account-net-value"><span>净资产（估值） <CircleHelp size={13} /></span><strong>{readableFact(accountFact) ? moneyText(account?.equity, currency) : "—"}</strong><div><b className={session?.pnlToday !== null && (session?.pnlToday ?? 0) >= 0 ? "reference-positive" : "reference-negative"}>{readableFact(sessionFact) ? signedMoney(session?.pnlToday, currency) : "—"}</b><b>{readableFact(sessionFact) ? "本时段" : ""}</b></div><AccountTrend points={trendPoints} fact={trendFact} /></div>
    <dl className="account-summary-list">
      <div><dt>可用资金</dt><dd>{readableFact(accountFact) ? moneyText(account?.freeMargin, currency) : "—"}</dd></div>
      <div><dt>本时段盈亏</dt><dd className={session?.pnlToday !== null && (session?.pnlToday ?? 0) >= 0 ? "reference-positive" : "reference-negative"}>{readableFact(sessionFact) ? signedMoney(session?.pnlToday, currency) : "—"}</dd></div>
      <div><dt>账户余额</dt><dd>{readableFact(accountFact) ? moneyText(account?.balance, currency) : "—"}</dd></div>
      <div><dt>保证金占用</dt><dd>{readableFact(accountFact) ? moneyText(account?.margin, currency) : "—"}</dd></div>
      <div><dt>风险指标 VaR(95%)</dt><dd><span className="reference-risk-pill">{riskValue === null || riskValue === undefined ? "—" : `${numberText(riskValue, 2)}%`}</span></dd></div>
    </dl>
  </section>;
}

function PnlOverview({ points, data, fact, scope, setScope }: { points: RealizedPnlPoint[]; data: RealizedPnlSeries | undefined; fact: FactEnvelope; scope: RealizedPnlScope; setScope: (scope: RealizedPnlScope) => void }) {
  const currency = data?.currency ?? null;
  const realized = readableFact(fact) ? data?.summary.realizedPnl : null;
  const isAllScope = scope === "all";
  const axisMode = scope === "7d" || scope === "30d" || isAllScope ? "date-time" : "clock";
  const rangeOptions: Array<{ value: RealizedPnlScope; label: string }> = [{ value: "today", label: "当日" }, { value: "24h", label: "24时" }, { value: "7d", label: "7日" }, { value: "30d", label: "30日" }, { value: "all", label: "全部" }];
  const chart = readableFact(fact)
    ? <PnlChart points={points} initialCapital={isAllScope ? DEFAULT_INITIAL_CAPITAL : 0} baselineLabel="起始权益" showBaseline={isAllScope} axisMode={axisMode} valueLabel={isAllScope ? "权益" : "累计盈亏"} emptyLabel={isAllScope ? "已确认暂无历史平仓记录，显示起始权益基线" : "已确认暂无该范围内平仓记录"} />
    : <div className="chart-empty"><span>{fact.state === "error" ? "盈亏事实读取失败，不显示猜测曲线" : "盈亏事实待确认，不显示猜测曲线"}</span></div>;
  return <section className="wb-panel reference-panel pnl-panel cockpit-pnl-panel pnl-overview-panel">
    <header className="reference-panel-header"><h2>盈亏曲线 <CircleHelp size={14} /></h2><FactBadge compact fact={fact} label="盈亏" /><span className="reference-unit">单位：{currency ?? "未知"}</span></header>
    <div className="pnl-legend"><span><i className="legend-main" />累计盈亏 <b>{realized === null || realized === undefined ? "—" : signedMoney(realized, currency)}</b></span><div className="reference-range-tabs">{rangeOptions.map((item) => <button key={item.value} type="button" className={scope === item.value ? "active" : ""} onClick={() => setScope(item.value)}>{item.label}</button>)}</div></div>
    {chart}
    <div className="reference-panel-source"><span>已实现记录：{readableFact(fact) ? `${data?.summary.trades ?? "—"} 笔` : "未知"}</span><SourceLine fact={fact} /></div>
  </section>;
}

function MarketOverview({ snapshot, spotFact, livePrices }: { snapshot: LiveStateSnapshot | null; spotFact: FactEnvelope; livePrices: number[] }) {
  const [activeTab, setActiveTab] = useState("现货");
  const liveBars = useQuery({
    queryKey: ["workbench", "market-live-bars", MARKET_SYMBOL, "M5"],
    queryFn: () => getMarketBars(MARKET_SYMBOL, "M5", 120, undefined, "live"),
    enabled: activeTab === "K线",
    staleTime: 4_000,
    refetchInterval: 5_000,
    retry: false,
  });
  const spot = snapshot?.spot;
  const hasSpot = readableFact(spotFact);
  const liveBarsFact = queryFact(liveBars.data?.fact, liveBars.error, "market.bars.v1", "ctrader_m5_trendbar_not_loaded");
  const hasLiveBars = activeTab === "K线" && readableFact(liveBarsFact) && liveBarsFact.source === "ctrader_live_trendbar" && Boolean(liveBars.data?.bars.length);
  const prices = hasSpot && spot?.mid !== null && spot?.mid !== undefined && Number.isFinite(spot.mid) && spot.mid > 0
    ? livePrices.length ? livePrices : [spot.mid]
    : [];
  const first = prices[0] ?? null;
  const latest = prices[prices.length - 1] ?? null;
  const trend = first !== null && latest !== null ? latest - first : null;
  const spread = hasSpot && spot?.bid !== null && spot?.bid !== undefined && spot?.ask !== null && spot?.ask !== undefined
    ? spot.ask - spot.bid
    : null;
  const liveBarsEmptyLabel = liveBars.isLoading
    ? "正在接收 cTrader M5 K 线…"
    : liveBars.error
      ? "cTrader M5 K 线读取失败"
      : liveBarsFact.source !== "ctrader_live_trendbar"
        ? "等待 cTrader M5 trendbar 推送，不显示月库替代数据"
        : "cTrader M5 K 线暂未确认";
  return <section className="wb-panel reference-panel market-overview-panel">
    <header className="reference-panel-header market-overview-header"><h2>市场概览</h2><div className="reference-tab-group">{["现货", "K线"].map((tab) => <button key={tab} type="button" className={activeTab === tab ? "active" : ""} onClick={() => setActiveTab(tab)}>{tab}</button>)}</div></header>
    {activeTab === "现货" ? <>
      <div className="market-table-head"><span>名称</span><span>实时价</span><span>点差</span></div>
      <div className="market-table"><div className="market-table-row"><strong>{MARKET_SYMBOL}</strong><Sparkline points={prices} tone={trend === null ? "neutral" : trend >= 0 ? "positive" : "negative"} /><span>{hasSpot ? numberText(spot?.mid, 2) : "—"}</span><b>{spread === null || !Number.isFinite(spread) ? "—" : numberText(spread, 2)}</b></div></div>
    </> : <>
      <div className="market-candle-meta"><span>{MARKET_SYMBOL} · 5分钟</span><FactBadge compact fact={liveBarsFact} label="K线" /></div>
      <MarketChart bars={hasLiveBars ? liveBars.data?.bars ?? [] : []} emptyLabel={liveBarsEmptyLabel} />
    </>}
    <div className="market-quote-strip"><span>买价　{hasSpot ? numberText(spot?.bid, 2) : "—"}</span><span>卖价　{hasSpot ? numberText(spot?.ask, 2) : "—"}</span><FactBadge compact fact={spotFact} label="现货" /></div>
    <div className="reference-panel-source"><span>{activeTab === "K线" ? "K线流：cTrader M5 Trendbar" : "报价流：cTrader Spot"}</span><SourceLine fact={activeTab === "K线" ? liveBarsFact : spotFact} /></div>
  </section>;
}

function PipelineStage({ stage }: { stage: ReceiptStage }) {
  return <div className={`pipeline-stage stage-${stage.state}`}>
    <div className="pipeline-stage-heading"><span className="pipeline-stage-icon"><ReceiptStateIcon state={stage.state} /></span><strong>{stage.label}</strong><em>{stage.statusText ?? receiptStateText(stage.state)}</em></div>
    <small>{stage.detail}</small>
    {stage.reason && <code>原因：{stage.reason}</code>}
  </div>;
}

function TradingTickets({ snapshot, policyFact, verdict }: { snapshot: LiveStateSnapshot | null; policyFact: FactEnvelope; verdict: RiskPolicyVerdict | null }) {
  const receipt = useMemo(() => buildPipelineReceipt(snapshot, policyFact, verdict), [policyFact, snapshot, verdict]);
  const computingReceipt = useMemo(() => buildComputingReceipt(snapshot), [snapshot]);
  const [history, setHistory] = useState<PipelineReceipt[]>([]);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setHistory((previous) => {
      let decisions = previous.filter((entry) => entry.kind === "decision");
      if (receipt) {
        const existingIndex = decisions.findIndex((entry) => entry.id === receipt.id);
        if (existingIndex >= 0) {
          decisions = decisions.map((entry, index) => index === existingIndex ? receipt : entry);
        } else {
          decisions = [...decisions, receipt].slice(-15);
        }
      }
      return computingReceipt ? [...decisions, computingReceipt].slice(-16) : decisions.slice(-16);
    });
  }, [computingReceipt, receipt]);

  useEffect(() => {
    const log = scrollRef.current;
    if (log) log.scrollTop = log.scrollHeight;
  }, [history]);

  const snapshotFact = snapshot?.fact ?? unavailableFact("live.state.v2", "live_snapshot_not_loaded");
  return <section className="wb-panel reference-panel trade-tickets-panel trade-actions cockpit-action-grid">
    <header className="reference-panel-header pipeline-receipt-header"><div><h2>因子管道票据 <small>因子 → 闸门 → 风控 → 开仓</small></h2><p>保留上一轮链路结果，并显示下一轮正在计算的票据。</p></div><div className="pipeline-receipt-header-meta"><span className="pipeline-live-indicator"><i />随因子更新</span><GitBranch size={17} /></div></header>
    <div className="pipeline-receipt-log" ref={scrollRef} aria-live="polite" aria-label="实时因子管道票据日志">
      {history.length ? history.map((entry) => <article className={`pipeline-receipt-entry pipeline-receipt-entry-${entry.kind}`} key={entry.id}>
        <div className="pipeline-receipt-entry-head"><div><ShieldCheck size={15} /><strong>{entry.symbol}</strong><span>{entry.direction}</span></div><time>{entry.kind === "computing" ? "计算中" : clockText(entry.observedAt)}</time></div>
        <div className="pipeline-receipt-stages">{entry.stages.map((stage) => <PipelineStage key={`${entry.id}-${stage.id}`} stage={stage} />)}</div>
        <div className="pipeline-receipt-summary"><span>结论</span><strong>{entry.summary}</strong></div>
      </article>) : <div className="pipeline-receipt-empty"><CircleDashed size={18} /><strong>等待 /ws/state 当前快照</strong><span>收到服务端因子管道事实后，票据会按时间追加并自动滚动。</span></div>}
    </div>
    <div className="reference-panel-source pipeline-receipt-source"><span>实时源：/ws/state · 票据按因子快照更新 · 风控：/api/risk/policy/verdicts（5 秒刷新）</span><FactBadge compact fact={snapshotFact} label="实时事实" /><SourceLine fact={policyFact} /></div>
  </section>;
}

function positionValue(position: Position, value: "volume" | "entryPrice" | "currentPrice" | "unrealizedPnl"): string {
  const raw = position[value];
  return raw === null || raw === undefined ? "—" : numberText(raw, value === "volume" ? 2 : 2);
}

function HoldingsOverview({ snapshot, positionsFact, accountFact }: { snapshot: LiveStateSnapshot | null; positionsFact: FactEnvelope; accountFact: FactEnvelope }) {
  const positions = snapshot?.positions.positions ?? null;
  const currency = snapshot?.account.currency;
  const readablePositions = readableFact(positionsFact) && positions !== null;
  const completeFloatingPnl = readablePositions && positions.length > 0 && positions.every((position) => position.unrealizedPnl !== null);
  const floatingPnl = completeFloatingPnl ? positions.reduce((total, position) => total + (position.unrealizedPnl ?? 0), 0) : null;
  return <section className="wb-panel reference-panel positions-panel cockpit-positions-panel holdings-overview-panel">
    <header className="reference-panel-header holdings-header"><h2>持仓与头寸</h2><FactBadge compact fact={positionsFact} label="仓位" /></header>
    <div className="holdings-table-wrap"><table className="holdings-table"><thead><tr><th>品种</th><th>持仓</th><th>均价</th><th>最新价</th><th>盈亏</th></tr></thead><tbody>{readablePositions && positions.length ? positions.map((position) => <tr key={position.id}><td><strong>{position.symbol || "未知品种"}</strong></td><td>{positionValue(position, "volume")}</td><td>{positionValue(position, "entryPrice")}</td><td>{positionValue(position, "currentPrice")}</td><td className={position.unrealizedPnl !== null && position.unrealizedPnl < 0 ? "reference-negative" : "reference-positive"}>{positionValue(position, "unrealizedPnl")}</td></tr>) : <tr><td colSpan={5}><div className="holdings-empty"><FactBadge compact fact={positionsFact} label="仓位" /><span>{readablePositions ? "当前无已确认持仓" : "仓位事实未知或读取失败，不显示零仓替代值"}</span></div></td></tr>}</tbody></table></div>
    <div className="portfolio-summary"><div><span>账户权益</span><strong>{readableFact(accountFact) ? moneyText(snapshot?.account.equity, currency) : "—"}</strong></div><div><span>未实现盈亏</span><strong className={floatingPnl !== null && floatingPnl < 0 ? "reference-negative" : "reference-positive"}>{signedMoney(floatingPnl, currency)}</strong></div><div><span>已确认持仓</span><strong>{readablePositions ? `${positions.length} 条` : "—"}</strong></div></div>
  </section>;
}

export function TradeOpsPage() {
  const live = useLiveState();
  const snapshot = live.snapshot;
  const [liveSpotPrices, setLiveSpotPrices] = useState<number[]>([]);
  const [realizedPnlScope, setRealizedPnlScope] = useState<RealizedPnlScope>("all");
  const realizedPnl = useQuery({ queryKey: ["workbench", "realized-pnl", realizedPnlScope], queryFn: () => getRealizedPnlSeries(realizedPnlScope), staleTime: 30_000, retry: false });
  const riskSummary = useQuery({ queryKey: ["workbench", "risk"], queryFn: getRiskSnapshot, staleTime: 30_000, refetchInterval: 30_000, retry: false });
  const policyVerdicts = useQuery({ queryKey: ["workbench", "risk-policy-verdicts", "trade-receipt"], queryFn: () => getRiskPolicyVerdicts(20), staleTime: 4_000, refetchInterval: 5_000, retry: false });
  const accountFact = snapshot?.account.fact ?? unavailableFact("live.account.v2", "live_account_not_loaded");
  const sessionFact = snapshot?.session.fact ?? unavailableFact("live.session-risk.v2", "live_session_not_loaded");
  const positionsFact = snapshot?.positions.fact ?? unavailableFact("live.positions.v2", "live_positions_not_loaded");
  const spotFact = snapshot?.spot.fact ?? unavailableFact("live.spot-quote.v1", "live_spot_not_loaded");
  const spotMid = snapshot?.spot.mid ?? null;
  const spotObservedAt = snapshot?.spot.fact.observed_at ?? null;
  const pnlFact = queryFact(realizedPnl.data?.fact, realizedPnl.error, "live.realized-pnl.v2", "realized_pnl_not_loaded");
  const riskFact = queryFact(riskSummary.data?.fact, riskSummary.error, "risk.summary.v2", "risk_summary_not_loaded");
  const policyFact = queryFact(policyVerdicts.data?.fact, policyVerdicts.error, "risk.policy-verdicts.v2", "policy_verdicts_not_loaded");
  const currentVerdict = useMemo(() => latestVerdict(policyVerdicts.data?.items ?? [], policyVerdicts.data?.prePolicySkips ?? []), [policyVerdicts.data]);
  const riskValue = readableFact(riskFact) ? riskSummary.data?.snapshot.var95.value ?? null : null;
  const pnlPoints = readableFact(pnlFact) ? realizedPnl.data?.points ?? [] : [];
  useEffect(() => {
    if (!readableFact(spotFact) || spotMid === null || !Number.isFinite(spotMid) || spotMid <= 0) return;
    setLiveSpotPrices((previous) => {
      const last = previous[previous.length - 1];
      if (last !== undefined && Math.abs(last - spotMid) < 1e-9) return previous;
      return [...previous, spotMid].slice(-48);
    });
  }, [spotFact.state, spotObservedAt, spotMid]);
  return <div className="workspace-page trade-ops-page reference-dashboard" aria-label="交易运营">
    <div className="reference-dashboard-grid workspace-grid trade-grid">
      <AccountOverview snapshot={snapshot} accountFact={accountFact} sessionFact={sessionFact} riskValue={riskValue} trendPoints={pnlPoints} trendFact={pnlFact} />
      <PnlOverview points={pnlPoints} data={realizedPnl.data} fact={pnlFact} scope={realizedPnlScope} setScope={setRealizedPnlScope} />
      <MarketOverview snapshot={snapshot} spotFact={spotFact} livePrices={liveSpotPrices} />
      <HoldingsOverview snapshot={snapshot} positionsFact={positionsFact} accountFact={accountFact} />
      <TradingTickets snapshot={snapshot} policyFact={policyFact} verdict={currentVerdict} />
    </div>
  </div>;
}

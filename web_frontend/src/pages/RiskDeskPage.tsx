import { useQuery } from "@tanstack/react-query";
import { ArrowDownRight, ArrowUpRight, Gauge, ShieldAlert } from "lucide-react";
import type { FactEnvelope, FactState } from "@/api/fact";
import { formatObservedTime } from "@/api/time";
import { getRiskDeskData } from "@/api/domains/risk";
import { FactBadge, MetricValue, Panel, SourceLine } from "@/design-system/primitives";
import { useLiveState } from "@/hooks/useLiveState";
import { WorkspaceTitle } from "@/workspaces/WorkspaceBits";
import { uiDecision, uiStatus } from "@/i18n/zh-CN";

const unknownFact = (contract: string, reasonCode: string): FactEnvelope => ({
  envelope: "fact.v1",
  contract,
  state: "unknown",
  source: "none",
  observed_at: null,
  generated_at: null,
  stale_after_sec: 0,
  reason_code: reasonCode,
  components: {},
});

function displayable(state: FactState): boolean {
  return state === "known" || state === "stale";
}

function Metric({ label, metric, parentState }: { label: string; metric: { status: FactState; value: number | null; unit: string; reasonCode: string | null }; parentState: FactState }) {
  const canDisplay = displayable(parentState) && displayable(metric.status);
  return <div className="risk-metric"><span>{label}</span><strong>{canDisplay ? <MetricValue value={metric.value} unit={metric.unit} /> : "—"}</strong><small>{uiStatus(metric.status)}{metric.reasonCode ? ` · ${metric.reasonCode}` : ""}</small></div>;
}

export function RiskDeskPage() {
  const query = useQuery({ queryKey: ["workbench", "risk-desk"], queryFn: getRiskDeskData, staleTime: 15_000, refetchInterval: 20_000, retry: false });
  const live = useLiveState();
  const data = query.data;
  const riskFact = data?.fact ?? unknownFact("risk.summary.v2", "risk_not_loaded");
  const policyFact = data?.policyFact ?? unknownFact("risk.policy-verdicts.v2", "policy_not_loaded");
  const traceFact = data?.traceFact ?? unknownFact("risk.trade-trace-recent.v2", "trade_trace_not_loaded");
  const positionsFact = live.snapshot?.positions.fact ?? unknownFact("live.positions.v2", "live_positions_not_loaded");
  const accountFact = live.snapshot?.account.fact ?? unknownFact("live.account.v2", "live_account_not_loaded");
  const safetyFact = live.snapshot?.safety ?? unknownFact("live.safety-freshness.v1", "live_safety_not_loaded");
  const riskMetric = { status: "unknown" as const, value: null, unit: "%", reasonCode: "snapshot_missing" };
  const account = live.snapshot?.account;
  const positionCount = displayable(positionsFact.state) ? String(live.snapshot?.positions.positions?.length ?? 0) : "—";
  const accountEquity = displayable(accountFact.state) && account && account.equity !== null && account.equity !== undefined
    ? `${account.currency ?? ""} ${account.equity.toFixed(2)}`.trim()
    : "—";
  const safetyValue = safetyFact.state === "known"
    ? live.snapshot?.loop.acceptingNewRisk === true ? "服务端允许" : live.snapshot?.loop.acceptingNewRisk === false ? "已阻止" : "—"
    : safetyFact.state === "stale" ? "待刷新" : "—";
  const verdictCount = displayable(policyFact.state) ? String(data?.verdicts.length ?? 0) : "—";
  const traceCount = displayable(traceFact.state) ? String(data?.traceRows.length ?? 0) : "—";

  return <div className="workspace-page risk-desk-page"><WorkspaceTitle kicker="02 / 风险权威" title="风险台" description="只读服务端风险摘要、政策裁决、压力结果和执行追踪。前端不计算任何风险或最终仓位。" fact={riskFact} /><div className="workspace-toolbar"><span><Gauge size={14} />风险 sizing / 服务端负责</span><span>策略 / RiskPolicyService</span><span>追踪 / 持久化执行证据</span></div><div className="reference-fact-strip risk-summary-strip">
    <div className="reference-fact-card"><span>风险快照</span><strong><FactBadge compact fact={riskFact} /></strong><small>{riskFact.reason_code ?? "服务端摘要"}</small></div>
    <div className="reference-fact-card"><span>已确认持仓</span><strong>{positionCount}</strong><small><FactBadge compact fact={positionsFact} /></small></div>
    <div className="reference-fact-card"><span>账户权益</span><strong>{accountEquity}</strong><small><FactBadge compact fact={accountFact} /></small></div>
    <div className="reference-fact-card"><span>新增风险安全门</span><strong>{safetyValue}</strong><small><FactBadge compact fact={safetyFact} /></small></div>
    <div className="reference-fact-card"><span>裁决 / 追踪</span><strong>{verdictCount} / {traceCount}</strong><small>只读记录数量</small></div>
  </div><div className="workspace-grid risk-grid">
    <Panel title="风险快照" eyebrow="/api/risk/summary" className="risk-snapshot-panel"><div className="status-banner"><FactBadge fact={riskFact} label="快照" /><span>{data?.snapshot.schemaVersion ?? "架构版本未知"}</span><span className="fact-inline-reason">{riskFact.reason_code ?? "没有已报告阻塞项"}</span></div><div className="risk-metric-grid"><Metric parentState={riskFact.state} label="VaR 95%" metric={data?.snapshot.var95 ?? riskMetric} /><Metric parentState={riskFact.state} label="CVaR 95%" metric={data?.snapshot.cvar95 ?? riskMetric} /><Metric parentState={riskFact.state} label="VaR 99% 影子" metric={data?.snapshot.var99 ?? riskMetric} /><Metric parentState={riskFact.state} label="CVaR 99% 影子" metric={data?.snapshot.cvar99 ?? riskMetric} /><Metric parentState={riskFact.state} label="压力损失" metric={data?.snapshot.stressLossPct ?? riskMetric} /><Metric parentState={riskFact.state} label="集中度" metric={data?.snapshot.concentrationPct ?? riskMetric} /><Metric parentState={riskFact.state} label="Kelly 投影" metric={data?.snapshot.kellyFraction ?? { ...riskMetric, unit: "fraction" }} /></div><SourceLine fact={riskFact} /></Panel>
    <Panel title="策略裁决" eyebrow="/api/risk/policy/verdicts" className="verdict-panel"><div className="panel-toolbar"><FactBadge fact={policyFact} label="策略" /></div>{displayable(policyFact.state) && data?.verdicts.length ? <div className="verdict-list">{data.verdicts.map((verdict) => <div className="verdict-row" key={verdict.id}><span className={`verdict-icon verdict-${verdict.decision}`}>{verdict.decision === "allow" ? <ArrowUpRight size={14} /> : <ArrowDownRight size={14} />}</span><strong>{verdict.action}</strong><span className={`decision-text decision-${verdict.decision}`}>{uiDecision(verdict.decision)}</span><code>{verdict.reasonCode ?? "reason_unknown"}</code><small>{formatObservedTime(verdict.decisionAt)}</small></div>)}</div> : <div className="empty-confirmed"><ShieldAlert size={15} />{policyFact.state === "known" ? "没有已确认策略裁决；不要把空列表解释为允许" : "策略事实未知；不把空列表解释为允许"}</div>}</Panel>
    <Panel title="持仓暴露" eyebrow="/ws/state" className="exposure-panel"><div className="metric-grid metric-grid-3"><div className="terminal-metric"><span>持仓事实</span><FactBadge compact fact={positionsFact} /><strong>{positionsFact.state === "known" || positionsFact.state === "stale" ? live.snapshot?.positions.positions?.length ?? 0 : "—"}</strong></div><div className="terminal-metric"><span>账户权益</span><FactBadge compact fact={accountFact} /><strong>{accountFact.state === "known" || accountFact.state === "stale" ? <MetricValue value={live.snapshot?.account.equity ?? null} /> : "—"}</strong></div><div className="terminal-metric"><span>新增风险安全门</span><FactBadge compact fact={safetyFact} /><strong>{safetyFact.state === "known" && live.snapshot?.loop.acceptingNewRisk === true ? "服务端允许" : safetyFact.state === "stale" ? "待刷新" : live.snapshot?.loop.acceptingNewRisk === false ? "已阻止" : "—"}</strong></div></div><p className="contract-note">暴露、集中度、最终数量和风险预算均来自服务端；本页没有客户端算式。</p></Panel>
    <Panel title="执行追踪" eyebrow="/api/risk/trade-trace/recent" className="trace-panel"><div className="panel-toolbar"><FactBadge fact={traceFact} label="追踪" /></div>{displayable(traceFact.state) && data?.traceRows.length ? <div className="trace-list">{data.traceRows.slice(0, 12).map((row) => <div className="trace-row" key={row.id}><span className="trace-stage">{row.stage}</span><strong>{row.action ?? row.symbol ?? "观测"}</strong><span>{uiStatus(row.outcome)}</span><code>{row.reasonCode ?? row.summary ?? (row.positionId ? `position:${row.positionId}` : "—")}</code><small>{[row.tradeId && `trade:${row.tradeId}`, row.positionId && `position:${row.positionId}`, formatObservedTime(row.observedAt)].filter(Boolean).join(" · ")}</small></div>)}</div> : <div className="empty-confirmed">{traceFact.state === "known" ? "执行追踪为空" : "执行追踪事实待服务端确认"}</div>}</Panel>
  </div></div>;
}

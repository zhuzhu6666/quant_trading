import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ExternalLink, Lock, Radio, RefreshCw } from "lucide-react";
import { getRealizedPnlSeries, submitEmergencyClose, submitStart, submitStop } from "@/api/workbench";
import type { FactEnvelope } from "@/api/fact";
import { DEFAULT_INITIAL_CAPITAL, PnlChart } from "@/design-system/PnlChart";
import { FactBadge, MetricValue, Panel, SourceLine } from "@/design-system/primitives";
import { useLiveState } from "@/hooks/useLiveState";
import { useNetworkStatus } from "@/hooks/useNetworkStatus";
import type { RealizedPnlScope } from "@/types/contracts";
import { ServerActionTicket, WorkspaceTitle } from "@/workspaces/WorkspaceBits";
import { uiDirection, uiStatus } from "@/i18n/zh-CN";

function FactNumber({ label, value, fact, unit = "" }: { label: string; value: number | null; fact: FactEnvelope; unit?: string }) {
  return <div className="terminal-metric"><span>{label}</span><FactBadge compact fact={fact} /><strong>{fact.state === "known" || fact.state === "stale" ? <MetricValue value={value} unit={unit} /> : "—"}</strong></div>;
}

function hasAccountValue(value: number | null | undefined, fact: FactEnvelope): value is number {
  return (fact.state === "known" || fact.state === "stale") && value !== null && value !== undefined && Number.isFinite(value);
}

function hasFreeMarginValue(value: number | null | undefined, fact: FactEnvelope): value is number {
  return hasAccountValue(value, fact) && value !== 0;
}

const unavailableFact = (contract: string, reasonCode: string, state: FactEnvelope["state"] = "unknown"): FactEnvelope => ({ envelope: "fact.v1", contract, state, source: "none", observed_at: null, generated_at: null, stale_after_sec: 0, reason_code: reasonCode, components: {} });
const queryFact = (fact: FactEnvelope | undefined, error: unknown, contract: string, reasonCode: string): FactEnvelope => fact ?? unavailableFact(contract, error ? `${reasonCode}_request_failed` : reasonCode, error ? "error" : "unknown");
const realizedPnlRanges: Array<{ value: RealizedPnlScope; label: string }> = [
  { value: "24h", label: "最近一天" },
  { value: "7d", label: "最近一周" },
  { value: "all", label: "全部" },
];

export function TradeOpsPage() {
  const live = useLiveState();
  const online = useNetworkStatus();
  const location = useLocation();
  const navigate = useNavigate();
  const [realizedPnlScope, setRealizedPnlScope] = useState<RealizedPnlScope>("all");
  const realizedPnl = useQuery({ queryKey: ["workbench", "realized-pnl", realizedPnlScope], queryFn: () => getRealizedPnlSeries(realizedPnlScope), staleTime: 30_000, retry: false });
  const snapshot = live.snapshot;

  useEffect(() => {
    const action = new URLSearchParams(location.search).get("action");
    if (action === "stop" || action === "emergency") {
      document.querySelector<HTMLElement>(".action-ticket")?.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }, [location.search]);

  const positions = snapshot?.positions.positions;
  const loopKnown = snapshot?.loop.fact.state === "known";
  const safetyFact = snapshot?.safety ?? unavailableFact("live.safety-freshness.v1", "live_safety_not_loaded");
  const gateStart = snapshot?.loop.acceptingNewRisk === true && safetyFact.state === "known" && loopKnown && online;
  const liveFact = snapshot?.fact ?? unavailableFact("live.state.v2", "live_state_not_loaded");
  const accountFact = snapshot?.account.fact ?? unavailableFact("live.account.v2", "live_account_not_loaded");
  const positionsFact = snapshot?.positions.fact ?? unavailableFact("live.positions.v2", "live_positions_not_loaded");
  const spotFact = snapshot?.spot.fact ?? unavailableFact("live.spot-quote.v1", "live_spot_not_loaded");
  const sessionFact = snapshot?.session.fact ?? unavailableFact("live.session-risk.v2", "live_session_not_loaded");
  const realizedPnlFact = queryFact(realizedPnl.data?.fact, realizedPnl.error, "live.realized-pnl.v2", "realized_pnl_not_loaded");
  const realizedPnlAvailable = realizedPnlFact.state === "known" || realizedPnlFact.state === "stale";
  const realizedPnlPoints = realizedPnlAvailable ? realizedPnl.data?.points ?? [] : [];
  const selectedPnlRange = realizedPnlRanges.find((range) => range.value === realizedPnlScope)?.label ?? "全部";
  const fullPnlScope = realizedPnlScope === "all";
  const pnlChartBaseline = fullPnlScope ? DEFAULT_INITIAL_CAPITAL : 0;

  return <div className="workspace-page trade-ops-page cockpit-page">
    <WorkspaceTitle kicker="01 / 市场工作台" title="交易运营" description="实时状态、行情、持仓和服务端复核的动作票据。安全与执行权始终留在后端。" fact={liveFact} />
    <div className="workspace-toolbar"><span><Radio size={14} />实时状态 / {uiStatus(live.connection)}</span><span>网络 / {online ? "在线" : "离线 · 只读"}</span><span>范围 / {selectedPnlRange}</span><span>原始资金 / {DEFAULT_INITIAL_CAPITAL.toFixed(2)} USD</span><button type="button" onClick={() => void live.refresh()}><RefreshCw size={14} />重新连接 WS</button></div>
    <div className="cockpit-event-strip" aria-label="实时数据流"><span className="cockpit-event-label">实时数据流</span><span className="cockpit-event-item"><b>01</b><FactBadge compact fact={liveFact} label="live.state.v2" /></span><span className="cockpit-event-item"><b>02</b><FactBadge compact fact={realizedPnlFact} label="live.realized-pnl.v2" /></span><span className="cockpit-event-item"><b>03</b><span>WS 重连 / {live.reconnectCount}</span></span></div>
    <div className="workspace-grid trade-grid">
      <Panel title="账户快照" eyebrow="服务端账户事实" className="live-status-panel cockpit-account-panel">
        <div className="account-metric-grid">
          {hasAccountValue(snapshot?.account.equity, accountFact) && <FactNumber label="账户权益" value={snapshot.account.equity} fact={accountFact} />}
          {hasFreeMarginValue(snapshot?.account.freeMargin, accountFact) && <FactNumber label="可用保证金" value={snapshot.account.freeMargin} fact={accountFact} />}
          {hasAccountValue(snapshot?.spot.mid, spotFact) && <FactNumber label="现货中间价" value={snapshot.spot.mid} fact={spotFact} />}
          {hasAccountValue(snapshot?.session.pnlToday, sessionFact) && <FactNumber label="本时段盈亏" value={snapshot.session.pnlToday} fact={sessionFact} />}
          {!hasAccountValue(snapshot?.account.equity, accountFact) && !hasFreeMarginValue(snapshot?.account.freeMargin, accountFact) && !hasAccountValue(snapshot?.spot.mid, spotFact) && !hasAccountValue(snapshot?.session.pnlToday, sessionFact) && <div className="account-metrics-empty">暂无可确认账户数据</div>}
        </div>
      </Panel>
      <Panel title="盈亏曲线" eyebrow="/api/live/realized-pnl-series" className="pnl-panel cockpit-pnl-panel"><div className="panel-toolbar"><FactBadge fact={realizedPnlFact} label="盈亏" /><span>{fullPnlScope ? `起始资金 ${DEFAULT_INITIAL_CAPITAL.toFixed(2)} USD` : "周期起点 0.00 USD · 不含原始资金"}</span><span>{realizedPnl.data?.summary.trades ?? "—"} 笔已实现</span><span>累计盈亏 {realizedPnl.data?.summary.realizedPnl === null || realizedPnl.data?.summary.realizedPnl === undefined ? "—" : realizedPnl.data.summary.realizedPnl.toFixed(2)}</span><div className="pnl-range-switcher" role="group" aria-label="盈亏曲线时间范围">{realizedPnlRanges.map((range) => <button key={range.value} type="button" className={realizedPnlScope === range.value ? "pnl-range-active" : ""} aria-pressed={realizedPnlScope === range.value} onClick={() => setRealizedPnlScope(range.value)}>{range.label}</button>)}</div></div><PnlChart points={realizedPnlPoints} initialCapital={DEFAULT_INITIAL_CAPITAL} baselineValue={pnlChartBaseline} baselineLabel={fullPnlScope ? "起始资金" : "周期起点"} showBaseline={realizedPnlAvailable} emptyLabel={realizedPnl.error ? "盈亏接口读取失败；未显示猜测曲线" : "盈亏事实待确认；不显示猜测曲线"} /><p className="chart-note">{fullPnlScope ? "全部范围显示账户权益：权益 = 原始资金 500.00 + 全历史累计已实现盈亏。" : "筛选范围显示该周期累计已实现盈亏；原始资金 500.00 仅属于全部范围，不在本周期重复计入。"} 未包含未平仓浮动盈亏。</p><SourceLine fact={realizedPnlFact} /></Panel>
      <Panel title="持仓" eyebrow="经纪商对账" className="positions-panel cockpit-positions-panel"><div className="panel-toolbar"><FactBadge fact={positionsFact} label="仓位" /><span>{positionsFact.state === "known" && positions ? `${positions.length} 条已确认记录` : positionsFact.state === "stale" && positions ? `${positions.length} 条已过期记录` : "记录数未知"}</span></div>{(positionsFact.state === "known" || positionsFact.state === "stale") && positions ? positions.length ? <div className="position-table">{positions.map((position) => <div className="position-row" key={position.id}><strong>{position.symbol || "XAUUSD+"}</strong><span className={position.direction === "long" ? "text-positive" : position.direction === "short" ? "text-negative" : "text-muted"}>{uiDirection(position.direction)}</span><span>数量 {position.volume ?? "—"}</span><span>开仓价 {position.entryPrice ?? "—"}</span><span>浮动盈亏 {position.unrealizedPnl ?? "—"}</span><button type="button" onClick={() => navigate(`/risk-desk?position=${encodeURIComponent(position.id)}`)}><ExternalLink size={13} />风险台</button></div>)}</div> : <div className="empty-confirmed">{positionsFact.state === "known" ? "当前无已确认持仓" : "仓位事实已过期；当前无可显示记录"}</div> : <div className="empty-confirmed"><Lock size={15} />持仓事实未知；不显示零仓或零风险替代值</div>}</Panel>
      <div className="trade-actions cockpit-action-grid"><ServerActionTicket title="停止新增风险" description="风险收紧动作由服务端复核；普通研究数据过期不会让风险缩减入口消失。" riskClass="risk-reduction" onSubmit={submitStop} /><ServerActionTicket title="紧急平仓" description="先由服务端锁定禁止新增风险，再做最新经纪商对账。未知仓位不会在前端猜测。" riskClass="risk-reduction" onSubmit={submitEmergencyClose} requiresStepUp /><ServerActionTicket title="启动实时循环" description="风险增加动作必须由服务端就绪度、权限、二次验证和动作门全部允许。" riskClass="risk-increase" onSubmit={submitStart} disabled={!gateStart} offline={!online} requiresStepUp /></div>
    </div>
  </div>;
}

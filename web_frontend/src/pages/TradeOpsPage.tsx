import { useEffect } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ExternalLink, Lock, Radio, RefreshCw } from "lucide-react";
import { getRealizedPnlSeries, getRiskSnapshot, submitEmergencyClose, submitStart, submitStop } from "@/api/workbench";
import type { FactEnvelope } from "@/api/fact";
import { formatTimestamp } from "@/api/time";
import { DEFAULT_INITIAL_CAPITAL, PnlChart } from "@/design-system/PnlChart";
import { FactBadge, MetricValue, Panel, SourceLine } from "@/design-system/primitives";
import { useLiveState } from "@/hooks/useLiveState";
import { useNetworkStatus } from "@/hooks/useNetworkStatus";
import { ServerActionTicket, WorkspaceTitle } from "@/workspaces/WorkspaceBits";
import { uiDirection, uiStatus } from "@/i18n/zh-CN";

function FactNumber({ label, value, fact, unit = "" }: { label: string; value: number | null; fact: FactEnvelope; unit?: string }) {
  return <div className="terminal-metric"><span>{label}</span><FactBadge compact fact={fact} /><strong>{fact.state === "known" || fact.state === "stale" ? <MetricValue value={value} unit={unit} /> : "—"}</strong></div>;
}

const unavailableFact = (contract: string, reasonCode: string, state: FactEnvelope["state"] = "unknown"): FactEnvelope => ({ envelope: "fact.v1", contract, state, source: "none", observed_at: null, generated_at: null, stale_after_sec: 0, reason_code: reasonCode, components: {} });
const queryFact = (fact: FactEnvelope | undefined, error: unknown, contract: string, reasonCode: string): FactEnvelope => fact ?? unavailableFact(contract, error ? `${reasonCode}_request_failed` : reasonCode, error ? "error" : "unknown");

export function TradeOpsPage() {
  const live = useLiveState();
  const online = useNetworkStatus();
  const location = useLocation();
  const navigate = useNavigate();
  const realizedPnl = useQuery({ queryKey: ["workbench", "realized-pnl", "all"], queryFn: () => getRealizedPnlSeries("all"), staleTime: 30_000, retry: false });
  const risk = useQuery({ queryKey: ["workbench", "risk"], queryFn: getRiskSnapshot, staleTime: 15_000, retry: false });
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
  const loopFact = snapshot?.loop.fact ?? unavailableFact("live.loop.v2", "live_loop_not_loaded");
  const spotFact = snapshot?.spot.fact ?? unavailableFact("live.spot-quote.v1", "live_spot_not_loaded");
  const sessionFact = snapshot?.session.fact ?? unavailableFact("live.session-risk.v2", "live_session_not_loaded");
  const riskFact = queryFact(risk.data?.fact, risk.error, "risk.summary.v2", "risk_not_loaded");
  const realizedPnlFact = queryFact(realizedPnl.data?.fact, realizedPnl.error, "live.realized-pnl.v2", "realized_pnl_not_loaded");
  const realizedPnlAvailable = realizedPnlFact.state === "known" || realizedPnlFact.state === "stale";
  const realizedPnlPoints = realizedPnlAvailable ? realizedPnl.data?.points ?? [] : [];

  return <div className="workspace-page trade-ops-page cockpit-page">
    <WorkspaceTitle kicker="01 / 市场工作台" title="交易运营" description="实时状态、行情、持仓和服务端复核的动作票据。安全与执行权始终留在后端。" fact={liveFact} />
    <div className="workspace-toolbar"><span><Radio size={14} />实时状态 / {uiStatus(live.connection)}</span><span>网络 / {online ? "在线" : "离线 · 只读"}</span><span>范围 / 全部已实现交易</span><span>初始资金 / {DEFAULT_INITIAL_CAPITAL.toFixed(2)} USD</span><button type="button" onClick={() => void live.refresh()}><RefreshCw size={14} />重新连接 WS</button></div>
    <div className="cockpit-event-strip" aria-label="实时数据流"><span className="cockpit-event-label">实时数据流</span><span className="cockpit-event-item"><b>01</b><FactBadge compact fact={liveFact} label="live.state.v2" /></span><span className="cockpit-event-item"><b>02</b><FactBadge compact fact={realizedPnlFact} label="live.realized-pnl.v2" /></span><span className="cockpit-event-item"><b>03</b><span>WS 重连 / {live.reconnectCount}</span></span></div>
    <div className="workspace-grid trade-grid">
      <Panel title="实时状态" eyebrow="服务端快照" className="live-status-panel cockpit-hero-panel">
        <div className="cockpit-hero-layout">
          <div className="cockpit-hero-summary"><span className="cockpit-hero-kicker">LIVE / WORKSPACE 01</span><div className="cockpit-hero-signal"><span className={`cockpit-orbit cockpit-orbit-${live.connection}`} aria-hidden="true"><span>{live.connection === "connected" ? "LIVE" : "—"}</span></span><div className="cockpit-hero-copy"><div className="status-banner"><FactBadge fact={liveFact} label="live.state.v2" /><span>{snapshot ? "首次完整快照已接收" : "首次完整快照待确认；不渲染猜测值"}</span></div><p>状态、风险和动作都以服务端事实为准；未知不会被显示成安全或零值。</p></div></div></div>
          <div className="metric-grid metric-grid-4"><FactNumber label="账户权益" value={snapshot?.account.equity ?? null} fact={accountFact} /><FactNumber label="可用保证金" value={snapshot?.account.freeMargin ?? null} fact={accountFact} /><FactNumber label="现货中间价" value={snapshot?.spot.mid ?? null} fact={spotFact} /><FactNumber label="本时段盈亏" value={snapshot?.session.pnlToday ?? null} fact={sessionFact} /></div>
        </div>
        <div className="status-list"><span><strong>经纪商</strong><em>{snapshot?.broker ?? "未知"}</em></span><span><strong>循环</strong><FactBadge compact fact={loopFact} /><em>{snapshot?.loop.running === true ? "运行中" : snapshot?.loop.running === false ? "已停止" : "未知"}</em></span><span><strong>新增风险</strong><FactBadge compact fact={safetyFact} /><em>{snapshot?.loop.acceptingNewRisk === true && safetyFact.state === "known" ? "服务端允许" : safetyFact.state === "stale" ? "安全门待刷新" : snapshot?.loop.acceptingNewRisk === false ? "已阻止" : "未知"}</em>{snapshot?.safetyBlockers.length ? <small>{snapshot.safetyBlockers[0]}</small> : null}</span></div>
      </Panel>
      <Panel title="盈亏曲线" eyebrow="/api/live/realized-pnl-series" className="pnl-panel cockpit-pnl-panel"><div className="panel-toolbar"><FactBadge fact={realizedPnlFact} label="盈亏" /><span>初始资金 {DEFAULT_INITIAL_CAPITAL.toFixed(2)} USD</span><span>{realizedPnl.data?.summary.trades ?? "—"} 笔已实现</span><span>累计盈亏 {realizedPnl.data?.summary.realizedPnl === null || realizedPnl.data?.summary.realizedPnl === undefined ? "—" : realizedPnl.data.summary.realizedPnl.toFixed(2)}</span></div><PnlChart points={realizedPnlPoints} initialCapital={DEFAULT_INITIAL_CAPITAL} showBaseline={realizedPnlAvailable} emptyLabel={realizedPnl.error ? "盈亏接口读取失败；未显示猜测曲线" : "盈亏事实待确认；不显示猜测曲线"} /><p className="chart-note">曲线显示账户权益：初始资金 500.00 USD；权益 = 500.00 + 累计已实现盈亏。未包含未平仓浮动盈亏。</p><SourceLine fact={realizedPnlFact} /></Panel>
      <Panel title="持仓" eyebrow="经纪商对账" className="positions-panel cockpit-positions-panel"><div className="panel-toolbar"><FactBadge fact={positionsFact} label="仓位" /><span>{positionsFact.state === "known" && positions ? `${positions.length} 条已确认记录` : positionsFact.state === "stale" && positions ? `${positions.length} 条已过期记录` : "记录数未知"}</span></div>{(positionsFact.state === "known" || positionsFact.state === "stale") && positions ? positions.length ? <div className="position-table">{positions.map((position) => <div className="position-row" key={position.id}><strong>{position.symbol || "XAUUSD+"}</strong><span className={position.direction === "long" ? "text-positive" : position.direction === "short" ? "text-negative" : "text-muted"}>{uiDirection(position.direction)}</span><span>数量 {position.volume ?? "—"}</span><span>开仓价 {position.entryPrice ?? "—"}</span><span>浮动盈亏 {position.unrealizedPnl ?? "—"}</span><button type="button" onClick={() => navigate(`/risk-desk?position=${encodeURIComponent(position.id)}`)}><ExternalLink size={13} />风险台</button></div>)}</div> : <div className="empty-confirmed">{positionsFact.state === "known" ? "当前无已确认持仓" : "仓位事实已过期；当前无可显示记录"}</div> : <div className="empty-confirmed"><Lock size={15} />持仓事实未知；不显示零仓或零风险替代值</div>}</Panel>
      <Panel title="风险上下文" eyebrow="服务端风险投影" className="risk-context-panel cockpit-risk-panel"><div className="risk-context-banner"><FactBadge fact={riskFact} label="risk.summary.v2" /><span>{risk.data?.snapshot.schemaVersion ?? "架构版本未知"}</span></div><div className="metric-grid metric-grid-3"><div className="terminal-metric"><span>VaR 95%</span><strong>{riskFact.state === "known" || riskFact.state === "stale" ? <MetricValue value={risk.data?.snapshot.var95.value ?? null} unit="%" /> : "—"}</strong></div><div className="terminal-metric"><span>CVaR 95%</span><strong>{riskFact.state === "known" || riskFact.state === "stale" ? <MetricValue value={risk.data?.snapshot.cvar95.value ?? null} unit="%" /> : "—"}</strong></div><div className="terminal-metric"><span>压力损失</span><strong>{riskFact.state === "known" || riskFact.state === "stale" ? <MetricValue value={risk.data?.snapshot.stressLossPct.value ?? null} unit="%" /> : "—"}</strong></div></div><p className="contract-note">VaR / CVaR / 压力 / sizing 由服务端计算；本面板只显示服务端投影。</p></Panel>
      <div className="trade-actions cockpit-action-grid"><ServerActionTicket title="停止新增风险" description="风险收紧动作由服务端复核；普通研究数据过期不会让风险缩减入口消失。" riskClass="risk-reduction" onSubmit={submitStop} /><ServerActionTicket title="紧急平仓" description="先由服务端锁定禁止新增风险，再做最新经纪商对账。未知仓位不会在前端猜测。" riskClass="risk-reduction" onSubmit={submitEmergencyClose} requiresStepUp /><ServerActionTicket title="启动实时循环" description="风险增加动作必须由服务端就绪度、权限、二次验证和动作门全部允许。" riskClass="risk-increase" onSubmit={submitStart} disabled={!gateStart} offline={!online} requiresStepUp /></div>
    </div>
  </div>;
}

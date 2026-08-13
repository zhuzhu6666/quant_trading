import { useQuery } from "@tanstack/react-query";
import { AlertOctagon, Bell, CloudOff, Cpu, Database, RefreshCw, ShieldAlert, Wifi } from "lucide-react";
import type { FactEnvelope } from "@/api/fact";
import { getAlerts, getHealth, getIncidentControl, getReadinessView, getRecovery, submitIncidentTighten } from "@/api/workbench";
import { readDesktopDiagnostics } from "@/desktop/bridge";
import { FactBadge, Panel, SourceLine } from "@/design-system/primitives";
import { useLiveState } from "@/hooks/useLiveState";
import { uiMode, uiStatus } from "@/i18n/zh-CN";
import { ServerActionTicket, WorkspaceTitle } from "@/workspaces/WorkspaceBits";

const unavailableFact = (contract: string, reasonCode: string, state: FactEnvelope["state"] = "unknown"): FactEnvelope => ({ envelope: "fact.v1", contract, state, source: "none", observed_at: null, generated_at: null, stale_after_sec: 0, reason_code: reasonCode, components: {} });
const queryFact = (fact: FactEnvelope | undefined, error: unknown, contract: string, reasonCode: string): FactEnvelope => fact ?? unavailableFact(contract, error ? `${reasonCode}_request_failed` : reasonCode, error ? "error" : "unknown");

function OpsFact({ icon, title, fact, value }: { icon: React.ReactNode; title: string; fact: FactEnvelope; value: string }) {
  return <div className="ops-fact"><span className="ops-fact-icon">{icon}</span><div><strong>{title}</strong><FactBadge compact fact={fact} /><small>{value}</small></div></div>;
}

export function OpsPage() {
  const live = useLiveState();
  const health = useQuery({ queryKey: ["ops", "health"], queryFn: getHealth, staleTime: 30_000, retry: false });
  const readiness = useQuery({ queryKey: ["workbench", "readiness"], queryFn: getReadinessView, staleTime: 15_000, retry: false });
  const incident = useQuery({ queryKey: ["ops", "incident"], queryFn: getIncidentControl, staleTime: 15_000, retry: false });
  const recovery = useQuery({ queryKey: ["ops", "recovery"], queryFn: getRecovery, staleTime: 30_000, retry: false });
  const alerts = useQuery({ queryKey: ["ops", "alerts"], queryFn: getAlerts, staleTime: 30_000, retry: false });
  const desktop = useQuery({ queryKey: ["ops", "desktop"], queryFn: readDesktopDiagnostics, staleTime: Infinity, retry: false });
  const healthFact = queryFact(health.data?.fact, health.error, "system.health.v2", "health_not_loaded");
  const readinessFact = queryFact(readiness.data?.fact, readiness.error, "ops.backend-readiness.v2", "readiness_not_loaded");
  const incidentFact = queryFact(incident.data?.fact, incident.error, "ops.incident-control.v2", "incident_not_loaded");
  const recoveryFact = queryFact(recovery.data?.fact, recovery.error, "ops.auto-recovery.v2", "recovery_not_loaded");
  const alertsFact = queryFact(alerts.data?.fact, alerts.error, "ops.alerts.v2", "alerts_not_loaded");
  const liveFact = live.snapshot?.fact ?? unavailableFact("live.state.v2", "live_state_not_loaded");
  const incidentMode = incident.data?.effectiveMode ?? "unknown";
  const liveDisplay = liveFact.state === "known"
    ? "数据正常"
    : liveFact.state === "stale"
      ? (live.connection === "connected" ? "WS 已连接 · 数据待刷新" : "数据待刷新")
      : live.connection === "connected"
        ? "WS 已连接 · 数据未知"
        : uiStatus(live.connection);

  return <div className="workspace-page ops-page">
    <WorkspaceTitle kicker="05 / 运维" title="运维中心" description="服务健康、后端就绪度、恢复、告警、事故控制、发布证据和桌面诊断。" fact={readinessFact} />
    <div className="workspace-toolbar">
      <span><Wifi size={14} />后端 / 服务端权威</span>
      <span>WS / {uiStatus(live.connection)}</span>
      <span>桌面端 / {desktop.data ? "Tauri" : "浏览器回退"}</span>
      <button type="button" onClick={() => { void live.refresh(); void health.refetch(); void readiness.refetch(); }}><RefreshCw size={14} />重新读取</button>
    </div>
    <div className="workspace-grid ops-grid">
      <Panel title="服务健康" eyebrow="/api/health" className="ops-health-panel">
        <div className="ops-fact-grid">
          <OpsFact icon={<Database size={16} />} title="后端" fact={healthFact} value={typeof health.data?.source.status === "string" ? uiStatus(health.data.source.status) : "未知"} />
          <OpsFact icon={<Wifi size={16} />} title="cTrader" fact={healthFact} value={typeof health.data?.source.ctrader === "string" ? uiStatus(health.data.source.ctrader) : "未知"} />
          <OpsFact icon={<Cpu size={16} />} title="实时状态" fact={liveFact} value={liveDisplay} />
          <OpsFact icon={<Bell size={16} />} title="告警" fact={alertsFact} value={alertsFact.state === "known" ? "服务端已返回" : "未知"} />
        </div>
        <SourceLine fact={healthFact} />
      </Panel>
      <Panel title="后端就绪度" eyebrow="/api/ops/backend-readiness" className="ops-readiness-panel">
        <div className="readiness-list">{readiness.data?.dimensions.length ? readiness.data.dimensions.map((dimension) => <div className="readiness-row" key={dimension.name}><strong>{dimension.name}</strong><span className={dimension.ready === true ? "text-positive" : dimension.ready === false ? "text-negative" : "text-muted"}>{dimension.ready === true ? "就绪" : dimension.ready === false ? "受阻" : "未知"}</span><code>{dimension.reasonCode ?? "reason_unknown"}</code></div>) : <div className="empty-confirmed">{readiness.error ? "就绪度读取失败；未显示猜测值" : "就绪度维度待确认"}</div>}</div>
        <div className="blocker-strip"><strong>阻塞项</strong><span>{readiness.data?.blockers.length ? readiness.data.blockers.join(" · ") : readinessFact.state === "known" ? "无" : "未知"}</span></div>
      </Panel>
      <Panel title="事故控制" eyebrow="/api/ops/incident-control" className="ops-incident-panel">
        <div className="incident-banner"><AlertOctagon size={20} /><div><strong>生效模式 / {uiMode(incidentMode)}</strong><span>本地安全闩锁 / {incident.data?.localSafetyLatch === true ? "已启用" : incident.data?.localSafetyLatch === false ? "未启用" : "未知"} · 配置模式 / {uiMode(incident.data?.configuredMode)}</span></div><FactBadge fact={incidentFact} /></div>
        <div className="incident-actions"><ServerActionTicket title="收紧为禁止新增风险" description="只允许服务端风险收紧；不提供 normal/thaw 路径。" riskClass="risk-reduction" onSubmit={() => submitIncidentTighten("no_new_risk")} /><ServerActionTicket title="收紧为仅允许平仓" description="已持仓风险缩减继续由服务端策略决定。" riskClass="risk-reduction" onSubmit={() => submitIncidentTighten("only_close")} /></div>
      </Panel>
      <Panel title="恢复与诊断" eyebrow="/api/ops/recovery + desktop" className="ops-recovery-panel">
        <div className="diagnostic-grid">
          <div><CloudOff size={16} /><div className="diagnostic-copy"><strong>恢复</strong><FactBadge fact={recoveryFact} /><small>{typeof recovery.data?.source.status === "string" ? uiStatus(recovery.data.source.status) : "未知"}</small></div></div>
          <div><ShieldAlert size={16} /><div className="diagnostic-copy"><strong>原因</strong><code>{recoveryFact.reason_code ?? "未知"}</code><small>不把未注册解释为健康</small></div></div>
          <div><Cpu size={16} /><div className="diagnostic-copy"><strong>WebView2</strong><span>{desktop.data?.webview2 ?? "浏览器 / 不可用"}</span><small>{desktop.data ? `${desktop.data.platform} ${desktop.data.architecture}` : "Tauri command 不可用"}</small></div></div>
        </div>
      </Panel>
    </div>
  </div>;
}

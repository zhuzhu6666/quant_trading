import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, AlertTriangle, Radio, ShieldCheck } from "lucide-react";
import { factAgeSeconds, type FactEnvelope } from "@/api/fact";
import { formatAgeSeconds, formatClock, formatObservedTime } from "@/api/time";
import { getIncidentControl, getReadinessView, getRiskSnapshot } from "@/api/workbench";
import { FactBadge, MetricValue } from "@/design-system/primitives";
import { useLiveState } from "@/hooks/useLiveState";
import { uiMode, uiStatus } from "@/i18n/zh-CN";

function unknownFact(contract: string, reasonCode: string): FactEnvelope {
  return { envelope: "fact.v1", contract, state: "unknown", source: "none", observed_at: null, generated_at: null, stale_after_sec: 0, reason_code: reasonCode, components: {} };
}

function displayable(fact: FactEnvelope): boolean {
  return fact.state === "known" || fact.state === "stale";
}

function timing(fact: FactEnvelope): string {
  return `观测 ${formatClock(fact.observed_at)} · ${formatAgeSeconds(factAgeSeconds(fact))}`;
}

export function SafetyRail({ onRefresh }: { onRefresh: () => void }) {
  const live = useLiveState();
  const queryClient = useQueryClient();
  const readiness = useQuery({ queryKey: ["workbench", "readiness"], queryFn: getReadinessView, staleTime: 15_000, retry: false });
  const risk = useQuery({ queryKey: ["workbench", "risk"], queryFn: getRiskSnapshot, staleTime: 15_000, retry: false });
  const incident = useQuery({ queryKey: ["ops", "incident"], queryFn: getIncidentControl, staleTime: 15_000, retry: false });
  const snapshot = live.snapshot;
  const riskFact = risk.data?.fact ?? unknownFact("risk.summary.v2", "risk_not_loaded");
  const readinessFact = readiness.data?.fact ?? unknownFact("ops.backend-readiness.v2", "readiness_not_loaded");
  const incidentFact = incident.data?.fact ?? unknownFact("ops.incident-control.v2", "incident_not_loaded");

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["workbench"] });
    void queryClient.invalidateQueries({ queryKey: ["ops", "incident"] });
    onRefresh();
  };

  return <header className="safety-rail" tabIndex={-1} aria-label="全局安全、就绪度与风险状态栏">
    <div className="rail-brand"><span className="rail-mark">Q</span><span><strong>QUANT</strong><small>WORKBENCH</small></span></div>
    <div className="rail-facts">
      <div className="rail-fact-group"><span className="rail-label"><Radio size={13} />实时状态</span><strong className={`rail-connection rail-${live.connection}`}>{uiStatus(live.connection)}</strong><small>{live.lastCompleteSnapshotAt ? `快照 ${formatObservedTime(live.lastCompleteSnapshotAt)}` : "首次完整快照待确认"}</small></div>
      <div className="rail-fact-group rail-fact-components"><span className="rail-label"><ShieldCheck size={13} />安全事实</span><div className="rail-chip-row"><FactBadge compact fact={snapshot?.account.fact ?? unknownFact("live.account.v2", "live_snapshot_missing")} label="账户" /><FactBadge compact fact={snapshot?.positions.fact ?? unknownFact("live.positions.v2", "live_snapshot_missing")} label="仓位" /><FactBadge compact fact={snapshot?.loop.fact ?? unknownFact("live.loop.v2", "live_snapshot_missing")} label="循环" /></div><small title={`账户 / ${snapshot?.account.fact.reason_code ?? "未知"} · 仓位 / ${snapshot?.positions.fact.reason_code ?? "未知"} · 循环 / ${snapshot?.loop.fact.reason_code ?? "未知"}`}>经纪商 / {snapshot?.broker ?? "未知"} · {timing(snapshot?.fact ?? unknownFact("live.state.v2", "live_snapshot_missing"))}</small></div>
      <div className="rail-fact-group"><span className="rail-label"><AlertTriangle size={13} />事故控制</span><div className="rail-chip-row"><FactBadge compact fact={incidentFact} /><strong>{uiMode(incident.data?.effectiveMode)}</strong></div><small>闩锁 / {incident.data?.localSafetyLatch === true ? "已启用" : incident.data?.localSafetyLatch === false ? "未启用" : "未知"} · {timing(incidentFact)}{incidentFact.reason_code ? ` · 原因 / ${incidentFact.reason_code}` : ""}</small></div>
      <div className="rail-fact-group"><span className="rail-label"><ShieldCheck size={13} />就绪度</span><FactBadge compact fact={readinessFact} label="服务端" /><div className="rail-readiness-row">{displayable(readinessFact) && readiness.data?.dimensions.length ? readiness.data.dimensions.slice(0, 4).map((dimension) => <span key={dimension.name} className={dimension.ready === true ? "rail-ready" : dimension.ready === false ? "rail-blocked" : "rail-unknown"}>{dimension.name}: {dimension.ready === true ? "就绪" : dimension.ready === false ? "受阻" : "未知"}</span>) : <span className="rail-unknown">维度未知</span>}</div><small>{readiness.data?.blockers[0] ? `阻塞 / ${readiness.data.blockers[0]} · ` : ""}{timing(readinessFact)}{readinessFact.reason_code ? ` · 原因 / ${readinessFact.reason_code}` : ""}</small></div>
      <div className="rail-fact-group rail-risk-summary"><span className="rail-label"><Activity size={13} />风险快照</span><FactBadge compact fact={riskFact} /><strong><MetricValue value={displayable(riskFact) ? risk.data?.snapshot.var95.value ?? null : null} unit="% VaR" /></strong><small>{timing(riskFact)}{riskFact.reason_code ? ` · 原因 / ${riskFact.reason_code}` : ""}</small></div>
    </div>
    <button className="rail-refresh" type="button" onClick={refresh} aria-label="重新读取服务端事实" title="重新读取服务端事实">↻</button>
  </header>;
}

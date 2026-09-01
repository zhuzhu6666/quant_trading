import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Gauge, Leaf, PieChart, Radio, ShieldCheck, Sun } from "lucide-react";
import { factAgeSeconds, type FactEnvelope } from "@/api/fact";
import { formatAgeSeconds, formatClock } from "@/api/time";
import { getHealth, getReadinessView } from "@/api/domains/ops";
import { getRiskSnapshot } from "@/api/domains/risk";
import { useLiveState, type LiveConnectionState } from "@/hooks/useLiveState";

function unknownFact(contract: string, reasonCode: string): FactEnvelope {
  return { envelope: "fact.v1", contract, state: "unknown", source: "none", observed_at: null, generated_at: null, stale_after_sec: 0, reason_code: reasonCode, components: {} };
}

function readable(fact: FactEnvelope): boolean {
  return fact.state === "known" || fact.state === "stale";
}

function factTiming(fact: FactEnvelope): string {
  return `观测 ${formatAgeSeconds(factAgeSeconds(fact))}`;
}

function readyText(ready: boolean | null | undefined): string {
  if (ready === true) return "已确认就绪";
  if (ready === false) return "服务端受阻";
  return "就绪度未知";
}

function liveTransportText(connection: LiveConnectionState): string {
  if (connection === "connected") return "实时通道已连接";
  if (connection === "connecting") return "实时通道连接中";
  if (connection === "auth-failed") return "实时认证失败";
  return "实时通道未连接";
}

function liveFactText(fact: FactEnvelope): string {
  if (fact.state === "known") return "业务事实已确认";
  if (fact.state === "stale") return "业务事实已过期";
  return "业务事实未确认";
}

export function SafetyRail({ onRefresh }: { onRefresh: () => void }) {
  const live = useLiveState();
  const queryClient = useQueryClient();
  const readiness = useQuery({ queryKey: ["workbench", "readiness"], queryFn: getReadinessView, staleTime: 15_000, retry: false });
  const risk = useQuery({ queryKey: ["workbench", "risk"], queryFn: getRiskSnapshot, staleTime: 30_000, refetchInterval: 30_000, retry: false });
  const health = useQuery({ queryKey: ["ops", "health"], queryFn: getHealth, staleTime: 30_000, refetchInterval: 60_000, retry: false });
  const snapshot = live.snapshot;
  const liveFact = snapshot?.fact ?? unknownFact("live.state.v2", "live_snapshot_not_loaded");
  const readinessFact = readiness.data?.fact ?? unknownFact("ops.backend-readiness.v2", "readiness_not_loaded");
  const riskFact = risk.data?.fact ?? unknownFact("risk.summary.v2", "risk_not_loaded");
  const healthFact = health.data?.fact ?? unknownFact("system.health.v2", health.error ? "health_request_failed" : "health_not_loaded");
  const sessionFact = snapshot?.session.fact ?? unknownFact("live.session-risk.v2", "live_session_not_loaded");
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["workbench"] });
    void queryClient.invalidateQueries({ queryKey: ["ops"] });
    onRefresh();
  };
  return <header className="safety-rail reference-safety-rail" tabIndex={-1} aria-label="服务端安全、就绪度与风险状态栏">
    <div className="rail-facts">
      <div className="rail-fact-group reference-status-card">
        <span className="rail-label"><ShieldCheck size={18} />系统就绪</span>
        <strong className={`rail-connection ${readable(healthFact) && health.data?.source.status === "ok" ? "rail-connected" : "rail-unknown"}`}>{readable(healthFact) ? (health.data?.source.status === "ok" ? "交易环境正常" : "健康状态受阻") : "健康事实未知"}</strong>
        <small>{factTiming(healthFact)}</small>
      </div>
      <div className="rail-fact-group reference-status-card">
        <span className="rail-label"><span className="rail-check-mark">✓</span>风控就绪</span>
        <strong className={`rail-connection ${readiness.data?.readyForLiveExecution === true ? "rail-connected" : "rail-unknown"}`}>{readyText(readiness.data?.readyForLiveExecution)}</strong>
        <small>{factTiming(readinessFact)}</small>
      </div>
      <div className="rail-fact-group reference-status-card reference-risk-status">
        <span className="rail-label"><Gauge size={18} />风险概览</span>
        <strong className="rail-status-value">VaR(95%)：{readable(riskFact) && risk.data?.snapshot.var95.value !== null ? `${risk.data?.snapshot.var95.value?.toFixed(2)}%` : "—"}</strong>
        <small>{factTiming(riskFact)}</small>
      </div>
      <div className="rail-fact-group reference-status-card reference-risk-status">
        <span className="rail-label reference-risk-label"><PieChart size={18} />今日风险</span>
        <strong className="rail-status-value">回撤：{readable(sessionFact) && snapshot?.session.drawdownPct !== null ? `${snapshot?.session.drawdownPct?.toFixed(2)}%` : "—"}</strong>
        <small>{factTiming(sessionFact)}</small>
      </div>
      <div className="rail-fact-group reference-status-card">
        <span className="rail-label"><Leaf size={18} />系统状态</span>
        <strong className={`rail-connection ${live.connection === "connected" ? "rail-connected" : "rail-unknown"}`}>{liveTransportText(live.connection)}</strong>
        <small>{liveFactText(liveFact)} · {factTiming(liveFact)}</small>
      </div>
    </div>
    <button className="rail-market-open" type="button" onClick={refresh} aria-label="刷新服务端状态">
      <Sun size={28} />
      <span>服务端时间<small>{formatClock(snapshot?.serverTime ?? health.data?.source.serverTime, "时间未知")}</small></span>
    </button>
    <span className="reference-preview-mark"><Radio size={11} />服务端事实</span>
  </header>;
}

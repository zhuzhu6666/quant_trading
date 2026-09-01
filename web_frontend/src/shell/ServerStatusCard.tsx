import { Cpu, HardDrive, MemoryStick, Server } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { getHealth, getSystemLoad } from "@/api/domains/ops";
import { uiStatus } from "@/i18n/zh-CN";
import { FactBadge } from "@/design-system/primitives";
import { formatClock } from "@/api/time";
import type { FactEnvelope } from "@/api/fact";
import type { SystemLoadView } from "@/types/contracts";

const unavailableFact = (contract: string, reasonCode: string, state: FactEnvelope["state"] = "unknown"): FactEnvelope => ({
  envelope: "fact.v1",
  contract,
  state,
  source: "none",
  observed_at: null,
  generated_at: null,
  stale_after_sec: 0,
  reason_code: reasonCode,
  components: {},
});

function formatPercent(value: number | null): string {
  return value === null ? "—" : `${Math.round(Math.max(0, Math.min(100, value)))}%`;
}

function formatBytes(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = Math.max(0, value);
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size >= 100 || unit === 0 ? Math.round(size) : size.toFixed(1)} ${units[unit]}`;
}

function statusText(healthFact: FactEnvelope, status: string | null): string {
  if (healthFact.state === "known" && status === "ok") return "服务正常";
  if (healthFact.state === "stale") return "健康数据已过期";
  if (healthFact.state === "error") return "健康读取失败";
  if (healthFact.state === "unknown") return "状态未知";
  return uiStatus(status);
}

function ResourceRow({ icon, label, percent, detail }: { icon: React.ReactNode; label: string; percent: number | null; detail: string }) {
  const width = percent === null ? 0 : Math.max(0, Math.min(100, percent));
  return <div className="sidebar-resource-row">
    <div className="sidebar-resource-label"><span>{icon}</span><strong>{label}</strong><b>{formatPercent(percent)}</b></div>
    <div className={`sidebar-resource-bar ${percent === null ? "sidebar-resource-bar-unknown" : ""}`} aria-hidden="true"><i style={percent === null ? undefined : { width: `${width}%` }} /></div>
    <small>{detail}</small>
  </div>;
}

function loadDetail(load: SystemLoadView | undefined, error: unknown): string {
  if (error) return "资源读取失败";
  if (!load) return "资源数据未知";
  return `采样 ${formatClock(load.observedAt, "时间未知")}`;
}

export function ServerStatusCard() {
  const health = useQuery({ queryKey: ["ops", "health"], queryFn: getHealth, staleTime: 30_000, refetchInterval: 60_000, retry: false });
  const load = useQuery({ queryKey: ["ops", "system-load"], queryFn: getSystemLoad, staleTime: 4_000, refetchInterval: 5_000, retry: false });
  const healthFact = health.data?.fact ?? unavailableFact("system.health.v2", health.error ? "health_request_failed" : "health_not_loaded", health.error ? "error" : "unknown");
  const systemLoad = load.data;

  return <section className="sidebar-server-status" aria-label="服务器状态">
    <div className="sidebar-server-header"><div className="sidebar-server-title"><Server size={14} aria-hidden="true" /><strong>服务器状态</strong></div><FactBadge fact={healthFact} compact /></div>
    <div className={`sidebar-server-summary sidebar-server-summary-${healthFact.state}`}><strong>{statusText(healthFact, health.data?.source.status ?? null)}</strong><small>{health.data?.source.serverTime ? `心跳 ${formatClock(health.data.source.serverTime)}` : "健康时间未知"}</small></div>
    <div className="sidebar-resource-list">
      <ResourceRow icon={<Cpu size={12} />} label="CPU" percent={systemLoad?.cpu.percent ?? null} detail={systemLoad?.cpu.cores ? `${systemLoad.cpu.cores} 核 · load ${systemLoad.cpu.load1?.toFixed(2) ?? "—"}` : "核心数未知"} />
      <ResourceRow icon={<MemoryStick size={12} />} label="内存" percent={systemLoad?.memory.percent ?? null} detail={systemLoad ? `${formatBytes(systemLoad.memory.availableBytes)} 可用` : "容量未知"} />
      <ResourceRow icon={<HardDrive size={12} />} label="磁盘" percent={systemLoad?.disk.percent ?? null} detail={systemLoad ? `${formatBytes(systemLoad.disk.freeBytes)} 可用` : "容量未知"} />
    </div>
    <small className="sidebar-server-meta">{loadDetail(systemLoad, load.error)}</small>
  </section>;
}

import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import { StatusPill } from "@/components/StatusPill";
import { FactEnvelope, factBoundTone, factHasDisplayValue, factViewLabel, factViewState } from "@/api/fact";

export type Tone = "ok" | "warn" | "bad" | "mute" | "pending" | "stale";

export function hasDisplayValue(value: ReactNode): boolean {
  if (value === null || value === undefined || value === false) return false;
  if (typeof value === "string") return !["", "", "—"].includes(value.trim());
  return true;
}

export function toneFromStatus(status: string): Tone {
  const normalized = status.toLowerCase();
  if (["ok", "healthy", "connected", "ready", "running", "active", "online"].includes(normalized)) return "ok";
  if (["degraded", "unknown", "idle", "warming", "limited", "warn"].includes(normalized)) return "warn";
  if (["error", "failed", "blocked", "down", "offline", "missing"].includes(normalized)) return "bad";
  if (normalized === "stale") return "stale";
  return "mute";
}

export function numberTone(value: number): Tone {
  if (value > 0) return "ok";
  if (value < 0) return "bad";
  return "mute";
}

export function StatTile({
  label,
  value,
  detail,
  tone = "mute",
  icon: Icon,
  fact,
  requestFailed = false,
}: {
  label: string;
  value: ReactNode;
  detail?: ReactNode;
  tone?: Tone;
  icon?: LucideIcon;
  fact?: FactEnvelope;
  requestFailed?: boolean;
}) {
  const displayValue = fact && !factHasDisplayValue(fact, requestFailed) ? "暂无实时数据" : value;
  const displayDetail = fact && !factHasDisplayValue(fact, requestFailed) ? undefined : detail;
  if (!hasDisplayValue(displayValue)) return null;
  const viewState = fact ? factViewState(fact, requestFailed) : null;
  const displayTone = fact ? factBoundTone(fact, tone, requestFailed) : tone;
  const factLabel = fact && viewState !== "known" ? factViewLabel(fact, requestFailed) : "";
  return (
    <div className={`stat-tile stat-${displayTone}${viewState ? ` fact-view-${viewState}` : ""}`}>
      <div className="stat-label">
        {Icon ? <Icon size={15} /> : null}
        <span>{label}</span>
      </div>
      <div className="stat-value">{displayValue}</div>
      {hasDisplayValue(displayDetail) || tone === "pending" || tone === "stale" || factLabel ? <div className="stat-detail">{displayDetail}{hasDisplayValue(displayDetail) && (factLabel || tone === "pending" || tone === "stale") ? " · " : null}{factLabel || (tone === "pending" ? "数据待确认" : tone === "stale" ? "数据已过期" : null)}</div> : null}
    </div>
  );
}

export function Field({ label, value, tone, fact, requestFailed = false }: { label: string; value: ReactNode; tone?: Tone; fact?: FactEnvelope; requestFailed?: boolean }) {
  const displayValue = fact && !factHasDisplayValue(fact, requestFailed) ? "暂无实时数据" : value;
  if (!hasDisplayValue(displayValue)) return null;
  return (
    <div className="field-row">
      <span>{label}</span>
      {tone ? <StatusPill status={String(displayValue)} tone={tone} fact={fact} requestFailed={requestFailed} /> : <strong>{displayValue}</strong>}
    </div>
  );
}

export function CompactMetric({
  label,
  value,
  detail,
  tone = "mute",
  className = "",
  fact,
  requestFailed = false,
}: {
  label: ReactNode;
  value: ReactNode;
  detail?: ReactNode;
  tone?: Tone;
  className?: string;
  fact?: FactEnvelope;
  requestFailed?: boolean;
}) {
  const displayValue = fact && !factHasDisplayValue(fact, requestFailed) ? "暂无实时数据" : value;
  const displayDetail = fact && !factHasDisplayValue(fact, requestFailed) ? undefined : detail;
  if (!hasDisplayValue(displayValue)) return null;
  const viewState = fact ? factViewState(fact, requestFailed) : null;
  const displayTone = fact ? factBoundTone(fact, tone, requestFailed) : tone;
  const factLabel = fact && viewState !== "known" ? factViewLabel(fact, requestFailed) : "";
  return (
    <div className={`compact-metric compact-metric-${displayTone}${viewState ? ` fact-view-${viewState}` : ""} ${className}`.trim()}>
      <span>{label}</span>
      <strong>{displayValue}</strong>
      {hasDisplayValue(displayDetail) || tone === "pending" || tone === "stale" || factLabel ? <small>{displayDetail}{hasDisplayValue(displayDetail) && (factLabel || tone === "pending" || tone === "stale") ? " · " : null}{factLabel || (tone === "pending" ? "数据待确认" : tone === "stale" ? "数据已过期" : null)}</small> : null}
    </div>
  );
}

export function ProgressMetric({
  label,
  value,
  detail,
  tone = "mute",
}: {
  label: string;
  value: number;
  detail?: ReactNode;
  tone?: Tone;
}) {
  const safeValue = Math.min(Math.max(Number.isFinite(value) ? value : 0, 0), 100);
  return (
    <div className={`progress-metric progress-${tone}`}>
      <div><span>{label}</span><strong>{safeValue.toFixed(0)}%</strong></div>
      <i aria-hidden="true"><b style={{ width: `${safeValue}%` }} /></i>
      {detail || tone === "pending" || tone === "stale" ? <small>{detail}{detail && (tone === "pending" || tone === "stale") ? " · " : null}{tone === "pending" ? "数据待确认" : tone === "stale" ? "数据已过期" : null}</small> : null}
    </div>
  );
}

export function SectionHead({
  title,
  status,
  tone = "mute",
}: {
  title: ReactNode;
  status?: string;
  tone?: Tone;
}) {
  return (
    <div className="section-head">
      <h3>{title}</h3>
      {status ? <StatusPill status={status} tone={tone} /> : null}
    </div>
  );
}

export function PageHeader({
  eyebrow,
  title,
  description,
  children,
}: {
  eyebrow: ReactNode;
  title: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <header className="dashboard-header">
      <div>
        <div className="eyebrow">{eyebrow}</div>
        <h1>{title}</h1>
        {description ? <p>{description}</p> : null}
      </div>
      {children ? <div className="page-header-actions">{children}</div> : null}
    </header>
  );
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <div className="empty-state-small" role="status">{children}</div>;
}

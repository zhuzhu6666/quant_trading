import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import { StatusPill } from "@/components/StatusPill";

export type Tone = "ok" | "warn" | "bad" | "mute" | "pending";

export function hasDisplayValue(value: ReactNode): boolean {
  if (value === null || value === undefined || value === false) return false;
  if (typeof value === "string") return !["", "", "—"].includes(value.trim());
  return true;
}

export function toneFromStatus(status: string): Tone {
  const normalized = status.toLowerCase();
  if (["ok", "healthy", "connected", "ready", "running", "active", "online"].includes(normalized)) return "ok";
  if (["degraded", "unknown", "idle", "warming", "limited", "warn"].includes(normalized)) return "warn";
  if (["error", "failed", "blocked", "down", "offline", "missing", "stale"].includes(normalized)) return "bad";
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
}: {
  label: string;
  value: ReactNode;
  detail?: ReactNode;
  tone?: Tone;
  icon?: LucideIcon;
}) {
  if (!hasDisplayValue(value)) return null;
  return (
    <div className={`stat-tile stat-${tone}`}>
      <div className="stat-label">
        {Icon ? <Icon size={15} /> : null}
        <span>{label}</span>
      </div>
      <div className="stat-value">{value}</div>
      {hasDisplayValue(detail) || tone === "pending" ? <div className="stat-detail">{detail}{hasDisplayValue(detail) && tone === "pending" ? " · " : null}{tone === "pending" ? "数据待确认" : null}</div> : null}
    </div>
  );
}

export function Field({ label, value, tone }: { label: string; value: ReactNode; tone?: Tone }) {
  if (!hasDisplayValue(value)) return null;
  return (
    <div className="field-row">
      <span>{label}</span>
      {tone ? <StatusPill status={String(value)} tone={tone} /> : <strong>{value}</strong>}
    </div>
  );
}

export function CompactMetric({
  label,
  value,
  detail,
  tone = "mute",
  className = "",
}: {
  label: ReactNode;
  value: ReactNode;
  detail?: ReactNode;
  tone?: Tone;
  className?: string;
}) {
  if (!hasDisplayValue(value)) return null;
  return (
    <div className={`compact-metric compact-metric-${tone} ${className}`.trim()}>
      <span>{label}</span>
      <strong>{value}</strong>
      {hasDisplayValue(detail) || tone === "pending" ? <small>{detail}{hasDisplayValue(detail) && tone === "pending" ? " · " : null}{tone === "pending" ? "数据待确认" : null}</small> : null}
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
      {detail || tone === "pending" ? <small>{detail}{detail && tone === "pending" ? " · " : null}{tone === "pending" ? "数据待确认" : null}</small> : null}
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

import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import { StatusPill } from "@/components/StatusPill";

export type Tone = "ok" | "warn" | "bad" | "mute";

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
  return (
    <div className={`stat-tile stat-${tone}`}>
      <div className="stat-label">
        {Icon ? <Icon size={15} /> : null}
        <span>{label}</span>
      </div>
      <div className="stat-value">{value}</div>
      {detail ? <div className="stat-detail">{detail}</div> : null}
    </div>
  );
}

export function Field({ label, value, tone }: { label: string; value: ReactNode; tone?: Tone }) {
  return (
    <div className="field-row">
      <span>{label}</span>
      {tone ? <StatusPill status={String(value || "--")} tone={tone} /> : <strong>{value || "--"}</strong>}
    </div>
  );
}

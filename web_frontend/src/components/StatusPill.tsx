import { translateDisplayValue } from "@/lib/display";

type StatusTone = "ok" | "warn" | "bad" | "mute" | "pending" | "stale";

const toneClass: Record<StatusTone, string> = {
  ok: "status-ok",
  warn: "status-warn",
  bad: "status-bad",
  mute: "status-mute",
  pending: "status-pending",
  stale: "status-stale",
};

export function StatusPill({ status, tone = "mute" }: { status: string; tone?: StatusTone }) {
  if (!status || ["", "—"].includes(status.trim())) return null;
  const safeTone = toneClass[tone] || toneClass.mute;
  const label = translateDisplayValue(status);
  const suffix = tone === "pending" && !label.includes("待确认")
    ? "数据待确认"
    : tone === "stale" && !label.includes("过期")
      ? "数据已过期"
      : "";
  return <span className={`status-pill ${safeTone}`}>{suffix ? `${label} · ${suffix}` : label}</span>;
};

export default StatusPill;

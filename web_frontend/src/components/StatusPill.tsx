import { translateDisplayValue } from "@/lib/display";

type StatusTone = "ok" | "warn" | "bad" | "mute" | "pending";

const toneClass: Record<StatusTone, string> = {
  ok: "status-ok",
  warn: "status-warn",
  bad: "status-bad",
  mute: "status-mute",
  pending: "status-pending",
};

export function StatusPill({ status, tone = "mute" }: { status: string; tone?: StatusTone }) {
  if (!status || ["", "—"].includes(status.trim())) return null;
  const safeTone = toneClass[tone] || toneClass.mute;
  const label = translateDisplayValue(status);
  return <span className={`status-pill ${safeTone}`}>{tone === "pending" ? `${label} · 数据待确认` : label}</span>;
};

export default StatusPill;

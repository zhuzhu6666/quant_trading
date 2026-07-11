import { translateDisplayValue } from "@/lib/display";

type StatusTone = "ok" | "warn" | "bad" | "mute";

const toneClass: Record<StatusTone, string> = {
  ok: "status-ok",
  warn: "status-warn",
  bad: "status-bad",
  mute: "status-mute",
};

export function StatusPill({ status, tone = "mute" }: { status: string; tone?: StatusTone }) {
  if (!status || ["", "—"].includes(status.trim())) return null;
  const safeTone = toneClass[tone] || toneClass.mute;
  return <span className={`status-pill ${safeTone}`}>{translateDisplayValue(status)}</span>;
};

export default StatusPill;

import { translateDisplayValue } from "@/lib/display";
import { FactEnvelope, factHasDisplayValue, factViewLabel, factViewState } from "@/api/fact";

type StatusTone = "ok" | "warn" | "bad" | "mute" | "pending" | "stale";

const toneClass: Record<StatusTone, string> = {
  ok: "status-ok",
  warn: "status-warn",
  bad: "status-bad",
  mute: "status-mute",
  pending: "status-pending",
  stale: "status-stale",
};

export function StatusPill({
  status,
  tone = "mute",
  fact,
  requestFailed = false,
}: {
  status: string;
  tone?: StatusTone;
  fact?: FactEnvelope;
  requestFailed?: boolean;
}) {
  if (!status || ["", "—"].includes(status.trim())) return null;
  const safeTone = toneClass[tone] || toneClass.mute;
  if (fact && !factHasDisplayValue(fact, requestFailed)) {
    return <span className={`status-pill ${safeTone} fact-view-${factViewState(fact, requestFailed)}`}>暂无实时数据</span>;
  }
  const label = translateDisplayValue(status);
  const viewState = fact ? factViewState(fact, requestFailed) : null;
  const factLabel = fact ? factViewLabel(fact, requestFailed) : "";
  const suffix = fact
    ? viewState !== "known" && !label.includes("待确认") && !label.includes("过期") && !label.includes("失败") && !label.includes("错误")
      ? factLabel
      : ""
    : tone === "pending" && !label.includes("待确认")
      ? "数据待确认"
      : tone === "stale" && !label.includes("过期")
        ? "数据已过期"
        : "";
  const factClass = viewState ? ` fact-view-${viewState}` : "";
  return <span className={`status-pill ${safeTone}${factClass}`}>{suffix ? `${label} · ${suffix}` : label}</span>;
};

export default StatusPill;

import { ReactNode } from "react";
import { AlertTriangle } from "lucide-react";
import { FactEnvelope, factViewState } from "@/api/fact";
import { formatTime } from "@/lib/format";

export function FactBoundary({
  fact,
  label,
  requestFailed = false,
  children,
}: {
  fact: FactEnvelope;
  label: string;
  requestFailed?: boolean;
  children: ReactNode;
}) {
  const viewState = factViewState(fact, requestFailed);
  if (viewState === "known") return <>{children}</>;
  return (
    <div className={`fact-boundary ${viewState === "stale" ? "fact-boundary-stale" : "fact-boundary-unknown"}`} role="status">
      <AlertTriangle size={14} />
      <span>{label}暂无实时数据{fact.reason_code ? ` · ${fact.reason_code}` : ""}{fact.observed_at ? ` · 最后观测 ${formatTime(fact.observed_at)}` : ""}</span>
    </div>
  );
}

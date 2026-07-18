import { ReactNode } from "react";
import { AlertTriangle, Clock3 } from "lucide-react";
import { FactEnvelope, factStatusLabel } from "@/api/fact";
import { formatTime } from "@/lib/format";

export function FactBoundary({
  fact,
  label,
  children,
}: {
  fact: FactEnvelope;
  label: string;
  children: ReactNode;
}) {
  if (fact.state === "known") return <>{children}</>;
  if (fact.state === "stale") {
    return (
      <div className="fact-boundary fact-boundary-stale">
        <div className="fact-boundary-note"><Clock3 size={13} />{label}已过期 · {formatTime(fact.observed_at)}</div>
        {children}
      </div>
    );
  }
  return (
    <div className="fact-boundary fact-boundary-unknown" role="status">
      <AlertTriangle size={14} />
      <span>{label}{factStatusLabel(fact)}{fact.reason_code ? ` · ${fact.reason_code}` : ""}</span>
    </div>
  );
}

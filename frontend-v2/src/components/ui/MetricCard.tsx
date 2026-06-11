import { classNames, fmtNum } from "@/lib/format";
import { Skeleton } from "./Skeleton";

interface MetricCardProps {
  label: string;
  value: string | number;
  subvalue?: string;
  trend?: "up" | "down" | "neutral";
  loading?: boolean;
  className?: string;
}

export function MetricCard({ label, value, subvalue, trend, loading, className }: MetricCardProps) {
  if (loading) {
    return (
      <div className={classNames("bg-white border border-[#dce0e6] rounded-lg p-3", className)}>
        <Skeleton variant="metric" />
      </div>
    );
  }

  return (
    <div className={classNames("bg-white border border-[#dce0e6] rounded-lg p-3", className)}>
      <div className="text-xs text-[#6e7681] font-medium uppercase tracking-wider">{label}</div>
      <div
        key={String(value)}
        className={classNames(
          "text-2xl font-bold num mt-0.5 num-enter",
          trend ? "" : "text-[#1a1e24]"
        )}
        style={trend ? { color: trend === "up" ? "#16a34a" : trend === "down" ? "#dc2626" : "#1a1e24" } : undefined}
      >
        {typeof value === "number" ? fmtNum(value) : value}
      </div>
      {subvalue && <div className="text-xs text-fg-muted mt-0.5">{subvalue}</div>}
    </div>
  );
}

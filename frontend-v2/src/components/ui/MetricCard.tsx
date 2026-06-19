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
      <div className={classNames("bg-white shadow-card rounded-3xl p-5", className)}>
        <Skeleton variant="metric" />
      </div>
    );
  }

  return (
    <div className={classNames("bg-white shadow-card rounded-3xl p-5", className)}>
      <div className="section-label">{label}</div>
      <div
        key={String(value)}
        className={classNames(
          "metric-value mt-1",
          trend ? "" : "text-text-primary"
        )}
        style={trend ? { color: trend === "up" ? "#34C759" : trend === "down" ? "#FF3B30" : "#1D1D1F" } : undefined}
      >
        {typeof value === "number" ? fmtNum(value) : value}
      </div>
      {subvalue && <div className="text-2xs text-text-secondary mt-1">{subvalue}</div>}
    </div>
  );
}

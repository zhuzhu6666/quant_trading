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
      <div className={classNames("bg-[#1c2128] border border-border rounded-lg p-3", className)}>
        <Skeleton variant="metric" />
      </div>
    );
  }

  return (
    <div className={classNames("bg-[#1c2128] border border-border rounded-lg p-3", className)}>
      <div className="text-xs text-fg-muted font-medium uppercase tracking-wider">{label}</div>
      <div
        key={String(value)}
        className={classNames(
          "text-2xl font-bold num mt-0.5 num-enter",
          // B5 fix: Card bg=#1c2128 深色, body color=#1a1e24 几乎不可见. 显式深主题前景.
          // 用 Tailwind class 而非 inline style 避免覆盖 trend 的 text-up/text-down 红绿.
          trend ? "" : "text-[#e6edf3]"
        )}
        style={trend ? { color: trend === "up" ? "#16a34a" : trend === "down" ? "#dc2626" : "#e6edf3" } : undefined}
      >
        {typeof value === "number" ? fmtNum(value) : value}
      </div>
      {subvalue && <div className="text-xs text-fg-muted mt-0.5">{subvalue}</div>}
    </div>
  );
}

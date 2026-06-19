import { MiniAreaChart } from "./MiniAreaChart";
import { classNames } from "@/lib/format";

interface KpiCardProps {
  label: string;
  value: string | number;
  subvalue?: string;
  trend?: "up" | "down" | "neutral";
  chart?: number[];
  className?: string;
  onClick?: () => void;
}

const trendConfig = {
  up: { color: "#34C759", icon: "↑" },
  down: { color: "#FF3B30", icon: "↓" },
  neutral: { color: "#86868B", icon: "" },
};

export function KpiCard({ label, value, subvalue, trend = "neutral", chart, className, onClick }: KpiCardProps) {
  const config = trendConfig[trend];
  const Comp = onClick ? "button" : "div";

  return (
    <Comp
      onClick={onClick}
      className={classNames(
        "card p-5 flex flex-col justify-between",
        onClick && "cursor-pointer",
        className
      )}
    >
      <div className="flex items-start justify-between">
        <div>
          <div className="section-label mb-2">{label}</div>
          <div className="metric-value" style={{ color: config.color }}>
            {value}
          </div>
          {subvalue && (
            <div className="flex items-center gap-1 mt-1 text-2xs text-text-secondary">
              {config.icon && <span style={{ color: config.color }}>{config.icon}</span>}
              <span>{subvalue}</span>
            </div>
          )}
        </div>
        {chart && chart.length > 0 && (
          <div className="w-20 h-10 flex-shrink-0 ml-3">
            <MiniAreaChart data={chart} color={config.color} />
          </div>
        )}
      </div>
    </Comp>
  );
}

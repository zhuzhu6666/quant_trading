interface KpiCardProps {
  label: string;
  value: string | number;
  subvalue?: string;
  trend?: "up" | "down" | "neutral";
  children?: React.ReactNode;
  className?: string;
}

const COLORS = {
  up: "#16a34a",
  down: "#dc2626",
  neutral: "#1a1e24",
};

export function KpiCard({ label, value, subvalue, trend = "neutral", children, className = "" }: KpiCardProps) {
  const color = COLORS[trend];
  return (
    <div className={`glass p-3 ${className}`}>
      <div className="text-[10px] text-fg-muted uppercase tracking-wider mb-0.5">{label}</div>
      <div className="flex items-center justify-between">
        <div>
          <div className="text-lg font-bold num" style={{ color }}>{value}</div>
          {subvalue && <div className="text-[10px] text-fg-muted mt-0.5">{subvalue}</div>}
        </div>
        {children}
      </div>
    </div>
  );
}

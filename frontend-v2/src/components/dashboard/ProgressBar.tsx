interface ProgressBarProps {
  value: number; // 0-100
  color?: string;
  bgColor?: string;
  height?: number;
  className?: string;
}

export function ProgressBar({
  value, color = "#3b82f6", bgColor = "#e5e7eb",
  height = 4, className = "",
}: ProgressBarProps) {
  return (
    <div className={`rounded-full overflow-hidden ${className}`} style={{ height, background: bgColor }}>
      <div className="h-full rounded-full transition-all duration-500" style={{ width: `${Math.min(value, 100)}%`, background: color }} />
    </div>
  );
}

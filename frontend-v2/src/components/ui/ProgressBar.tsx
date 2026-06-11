interface ProgressBarProps {
  pct: number;
  status?: string;
  step?: string;
  className?: string;
}

export function ProgressBar({ pct, status, step, className }: ProgressBarProps) {
  return (
    <div className={`flex flex-col gap-1.5 ${className ?? ""}`}>
      <div className="flex justify-between items-center">
        <span className="text-xs text-fg-muted">{status ?? ""}</span>
        <span className="text-xs text-fg-muted num">{pct.toFixed(0)}%</span>
      </div>
      <div className="h-1.5 bg-[#e4e9f0] rounded-full overflow-hidden">
        <div className="h-full bg-accent rounded-full transition-all duration-500" style={{ width: `${pct}%` }} />
      </div>
      {step && <span className="text-xs text-fg-muted">{step}</span>}
    </div>
  );
}

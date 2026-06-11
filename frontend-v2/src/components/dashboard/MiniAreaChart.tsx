interface MiniAreaChartProps {
  data: number[];
  height?: number;
  color?: string;
  className?: string;
}

export function MiniAreaChart({ data, height = 36, color = "#3b82f6", className = "" }: MiniAreaChartProps) {
  if (data.length === 0) return null;
  const max = Math.max(...data, 1);
  const min = Math.min(...data, 0);
  const range = max - min || 1;
  const w = 100;
  const h = height;
  const step = w / (data.length - 1);

  const linePath = data.map((v, i) => {
    const x = i * step;
    const y = h - ((v - min) / range) * (h - 4) - 2;
    return `${i === 0 ? "M" : "L"}${x},${y}`;
  }).join(" ");

  const areaPath = `${linePath} L${w},${h} L0,${h} Z`;

  return (
    <svg width="100%" height="100%" viewBox={`0 0 ${w} ${h}`} className={className} preserveAspectRatio="xMidYMid meet">
      <defs>
        <linearGradient id={`area-${color.replace("#", "")}`} x1="0" x2="0" y1="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.2" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={areaPath} fill={`url(#area-${color.replace("#", "")})`} />
      <path d={linePath} fill="none" stroke={color} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

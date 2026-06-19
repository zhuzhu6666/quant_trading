interface DualRingProps {
  outerValue: number;
  outerLabel: string;
  innerValue: number;
  innerLabel: string;
  size?: number;
  className?: string;
}

function interpolateColor(value: number, thresholds: [number, number, number], colors: [string, string, string]): string {
  const [low, mid, high] = thresholds;
  const [red, yellow, green] = colors;
  if (value >= high) return green;
  if (value >= mid) return yellow;
  if (value <= low) return red;
  if (value > low && value < mid) {
    const t = (value - low) / (mid - low);
    return lerpColor(red, yellow, t);
  }
  const t = (value - mid) / (high - mid);
  return lerpColor(yellow, green, t);
}

function lerpColor(a: string, b: string, t: number): string {
  const ah = parseInt(a.replace("#", ""), 16);
  const bh = parseInt(b.replace("#", ""), 16);
  const ar = (ah >> 16) & 0xff, ag = (ah >> 8) & 0xff, ab = ah & 0xff;
  const br = (bh >> 16) & 0xff, bg = (bh >> 8) & 0xff, bb = bh & 0xff;
  const rr = Math.round(ar + (br - ar) * t);
  const rg = Math.round(ag + (bg - ag) * t);
  const rb = Math.round(ab + (bb - ab) * t);
  return `#${((1 << 24) | (rr << 16) | (rg << 8) | rb).toString(16).slice(1)}`;
}

export function DualRing({
  outerValue,
  outerLabel = "胜率",
  innerValue,
  innerLabel = "回撤",
  size = 64,
  className = "",
}: DualRingProps) {
  const cx = size / 2;
  const outerR = (size / 2) - 4;
  const innerR = (size / 2) - 12;
  const outerCirc = 2 * Math.PI * outerR;
  const innerCirc = 2 * Math.PI * innerR;
  const outerDash = (outerValue / 100) * outerCirc;
  const innerDash = Math.min(innerValue / 30, 1) * innerCirc;

  const outerColor = interpolateColor(outerValue, [30, 50, 70], ["#FF3B30", "#FF9500", "#34C759"]);
  const innerColor = interpolateColor(innerValue, [3, 5, 15], ["#34C759", "#FF9500", "#FF3B30"]);

  return (
    <div className={`flex flex-col items-center ${className}`}>
      <div className="relative" style={{ width: size, height: size }}>
        <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
          <circle cx={cx} cy={cx} r={outerR} fill="none" stroke="#E5E5EA" strokeWidth="4.5" />
          <circle
            cx={cx} cy={cx} r={outerR} fill="none" stroke={outerColor} strokeWidth="4.5"
            strokeDasharray={`${outerDash} ${outerCirc}`}
            transform={`rotate(-90,${cx},${cx})`}
            style={{ transition: "stroke-dasharray 0.8s ease-out, stroke 0.4s ease-out" }}
          />
          <circle cx={cx} cy={cx} r={innerR} fill="none" stroke="#E5E5EA" strokeWidth="3.5" />
          <circle
            cx={cx} cy={cx} r={innerR} fill="none" stroke={innerColor} strokeWidth="3.5"
            strokeDasharray={`${innerDash} ${innerCirc}`}
            strokeDashoffset={-innerCirc * 0.25}
            transform={`rotate(-90,${cx},${cx})`}
            style={{ transition: "stroke-dasharray 0.8s ease-out, stroke 0.4s ease-out" }}
          />
        </svg>
        <div className="absolute inset-0 flex flex-col items-center justify-center">
          <span className="text-sm font-bold num" style={{ color: outerColor }}>{outerValue}%</span>
          <span className="text-2xs num" style={{ color: innerColor }}>{innerValue}%</span>
        </div>
      </div>
      <div className="flex gap-3 mt-2 text-2xs text-text-secondary">
        <span className="flex items-center gap-1">
          <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: outerColor }} />
          {outerLabel}
        </span>
        <span className="flex items-center gap-1">
          <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: innerColor }} />
          {innerLabel}
        </span>
      </div>
    </div>
  );
}

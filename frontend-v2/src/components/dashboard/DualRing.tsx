interface DualRingProps {
  outerValue: number;
  outerLabel: string;
  innerValue: number;
  innerLabel: string;
  size?: number;
  className?: string;
}

function interpolateColor(value: number, thresholds: [number, number, number], colors: [string, string, string]): string {
  // thresholds: [low, mid, high], colors: [red, yellow, green]
  const [low, mid, high] = thresholds;
  const [red, yellow, green] = colors;
  if (value >= high) return green;
  if (value >= mid) return yellow;
  if (value <= low) return red;
  if (value > low && value < mid) {
    // Interpolate red → yellow
    const t = (value - low) / (mid - low);
    return lerpColor(red, yellow, t);
  }
  // Interpolate yellow → green
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
  outerValue, outerLabel = "胜率",
  innerValue, innerLabel = "回撤",
  size = 72, className = "",
}: DualRingProps) {
  const cx = size / 2;
  const outerR = (size / 2) - 5;
  const innerR = (size / 2) - 13;
  const outerCirc = 2 * Math.PI * outerR;
  const innerCirc = 2 * Math.PI * innerR;
  const outerDash = (outerValue / 100) * outerCirc;
  const innerDash = Math.min(innerValue / 30, 1) * innerCirc;

  const outerColor = interpolateColor(outerValue, [30, 50, 70], ["#dc2626", "#d97706", "#16a34a"]);
  const innerColor = interpolateColor(innerValue, [3, 5, 15], ["#16a34a", "#d97706", "#dc2626"]);

  return (
    <div className={`flex flex-col items-center ${className}`}>
      <div className="relative" style={{ width: size, height: size }}>
        <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
          {/* Outer ring bg */}
          <circle cx={cx} cy={cx} r={outerR} fill="none" stroke="#e5e7eb" strokeWidth="5" />
          {/* Outer ring fill */}
          <circle cx={cx} cy={cx} r={outerR} fill="none" stroke={outerColor} strokeWidth="5"
            strokeDasharray={`${outerDash} ${outerCirc}`}
            transform={`rotate(-90,${cx},${cx})`}
            style={{ transition: "stroke-dasharray 0.6s, stroke 0.3s" }}
          />
          {/* Inner ring bg */}
          <circle cx={cx} cy={cx} r={innerR} fill="none" stroke="#e5e7eb" strokeWidth="4" />
          {/* Inner ring fill */}
          <circle cx={cx} cy={cx} r={innerR} fill="none" stroke={innerColor} strokeWidth="4"
            strokeDasharray={`${innerDash} ${innerCirc}`}
            strokeDashoffset={-innerCirc * 0.25}
            transform={`rotate(-90,${cx},${cx})`}
            style={{ transition: "stroke-dasharray 0.6s, stroke 0.3s" }}
          />
        </svg>
        <div className="absolute inset-0 flex flex-col items-center justify-center">
          <span className="text-xs font-bold num" style={{ color: outerColor }}>{outerValue}%</span>
          <span className="text-[9px] num" style={{ color: innerColor }}>{innerValue}%</span>
        </div>
      </div>
      <div className="flex gap-3 mt-1 text-[9px] text-fg-muted">
        <span><span style={{ color: outerColor }}>●</span> {outerLabel}</span>
        <span><span style={{ color: innerColor }}>●</span> {innerLabel}</span>
      </div>
    </div>
  );
}

import { LineChart } from "lucide-react";
import { formatClock, formatTimestamp } from "@/api/time";
import type { RealizedPnlPoint } from "@/types/contracts";

export const DEFAULT_INITIAL_CAPITAL = 500;

type PnlChartProps = {
  points: RealizedPnlPoint[];
  initialCapital?: number;
  showBaseline?: boolean;
  emptyLabel?: string;
};

function money(value: number): string {
  return value.toFixed(2);
}

export function PnlChart({
  points,
  initialCapital = DEFAULT_INITIAL_CAPITAL,
  showBaseline = false,
  emptyLabel = "暂无已确认盈亏记录",
}: PnlChartProps) {
  if (!points.length && !showBaseline) {
    return <div className="chart-empty"><LineChart size={24} /><span>{emptyLabel}</span></div>;
  }

  const width = 960;
  const height = 300;
  const plotTop = 14;
  const plotBottom = 28;
  const plotLeft = 8;
  const plotRight = width - 8;
  const plotHeight = height - plotTop - plotBottom;
  const equityValues = points.map((point) => initialCapital + point.cumulative);
  const values = [initialCapital, ...equityValues];
  const rawMax = Math.max(...values);
  const rawMin = Math.min(...values);
  const padding = Math.max((rawMax - rawMin) * 0.14, Math.abs(initialCapital) * 0.004, 1);
  const max = rawMax + padding;
  const min = rawMin - padding;
  const range = Math.max(max - min, 0.00001);
  const scale = (value: number) => plotTop + ((max - value) / range) * plotHeight;
  const step = points.length ? (plotRight - plotLeft) / points.length : 0;
  const coordinates = [
    { x: plotLeft, y: scale(initialCapital), value: initialCapital, ts: null as number | null, pnl: 0 },
    ...points.map((point, index) => ({
      x: plotLeft + (index + 1) * step,
      y: scale(initialCapital + point.cumulative),
      value: initialCapital + point.cumulative,
      ts: point.ts,
      pnl: point.pnl,
    })),
  ];
  const linePath = coordinates.map((coordinate, index) => `${index === 0 ? "M" : "L"} ${coordinate.x.toFixed(2)} ${coordinate.y.toFixed(2)}`).join(" ");
  const first = coordinates[0];
  const last = coordinates[coordinates.length - 1];
  const areaPath = `${linePath} L ${last.x.toFixed(2)} ${height - plotBottom} L ${first.x.toFixed(2)} ${height - plotBottom} Z`;
  const gridValues = [0, 0.25, 0.5, 0.75, 1];
  const timeIndexes = [0, Math.floor((points.length - 1) / 2), points.length - 1]
    .filter((value, index, valuesForLabels) => points.length > 0 && valuesForLabels.indexOf(value) === index);
  const positive = last.value >= initialCapital;
  const baselineY = scale(initialCapital);

  return <svg className="pnl-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="以 500 为初始资金的已实现盈亏权益折线图">
    <g className="chart-grid" aria-hidden="true">
      {gridValues.map((ratio) => {
        const y = plotTop + ratio * plotHeight;
        const value = max - ratio * range;
        return <g key={ratio}><line x1={plotLeft} x2={plotRight} y1={y} y2={y} className="chart-grid-line" /><text x={plotRight - 1} y={y + 4} textAnchor="end" className="chart-axis-label">{money(value)}</text></g>;
      })}
    </g>
    <line x1={plotLeft} y1={baselineY} x2={plotRight} y2={baselineY} className="pnl-baseline" />
    <text x={plotLeft + 4} y={baselineY - 6} className="pnl-baseline-label">起始 {money(initialCapital)}</text>
    <path d={areaPath} className={`pnl-area ${positive ? "pnl-area-positive" : "pnl-area-negative"}`} />
    <path d={linePath} className={`pnl-line ${positive ? "pnl-line-positive" : "pnl-line-negative"}`} />
    <circle cx={first.x} cy={first.y} r="3" className="pnl-point pnl-point-start" />
    {points.length > 0 && <circle cx={last.x} cy={last.y} r="4" className={`pnl-point ${positive ? "pnl-point-positive" : "pnl-point-negative"}`} />}
    {points.map((point, index) => {
      const coordinate = coordinates[index + 1];
      return <title key={`${point.ts}-${index}`}>{`${formatTimestamp(point.ts)} · 单笔盈亏 ${money(point.pnl)} · 权益 ${money(coordinate.value)}`}</title>;
    })}
    {timeIndexes.map((index) => {
      const point = points[index];
      const coordinate = coordinates[index + 1];
      return <text key={`time-${index}`} x={coordinate.x} y={height - 8} textAnchor="middle" className="chart-time-label">{formatClock(point.ts)}</text>;
    })}
  </svg>;
}

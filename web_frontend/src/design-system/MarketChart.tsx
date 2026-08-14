import { CandlestickChart } from "lucide-react";
import type { Bar } from "@/types/contracts";
import { formatClock, formatTimestamp } from "@/api/time";

type MarketBar = Pick<Bar, "t" | "o" | "h" | "l" | "c">;
export type MarketChartFocus = { t: number; label: string };
export type MarketChartMarker = MarketChartFocus & { tone: "entry" | "exit" };

export function MarketChart({ bars, emptyLabel = "暂无已确认 K 线", focus, markers }: { bars: MarketBar[]; emptyLabel?: string; focus?: MarketChartFocus; markers?: MarketChartMarker[] }) {
  if (!bars.length) return <div className="chart-empty"><CandlestickChart size={24} /><span>{emptyLabel}</span></div>;

  const width = 960;
  const height = 300;
  const plotTop = 14;
  const plotBottom = 28;
  const plotHeight = height - plotTop - plotBottom;
  const highs = bars.map((bar) => bar.h);
  const lows = bars.map((bar) => bar.l);
  const max = Math.max(...highs);
  const min = Math.min(...lows);
  const range = Math.max(max - min, 0.00001);
  const scale = (value: number) => plotTop + ((max - value) / range) * plotHeight;
  const plotWidth = width - 16;
  const step = plotWidth / bars.length;
  const candleWidth = Math.max(3, Math.min(12, step * 0.58));
  const gridValues = [0, 0.25, 0.5, 0.75, 1];
  const timeIndexes = [0, Math.floor((bars.length - 1) / 2), bars.length - 1].filter((value, index, values) => values.indexOf(value) === index);
  const xForTimestamp = (timestamp: number): number => {
    if (bars.length === 1 || timestamp <= bars[0].t) return 8 + step / 2;
    for (let index = 1; index < bars.length; index += 1) {
      if (timestamp <= bars[index].t) {
        const previous = bars[index - 1];
        const interval = Math.max(1, bars[index].t - previous.t);
        const ratio = Math.max(0, Math.min(1, (timestamp - previous.t) / interval));
        return 8 + (index - 1 + ratio) * step + step / 2;
      }
    }
    return 8 + (bars.length - 1) * step + step / 2;
  };
  const chartMarkers = (markers?.length ? markers : focus ? [{ ...focus, tone: "entry" as const }] : [])
    .filter((marker) => Number.isFinite(marker.t))
    .map((marker) => ({ ...marker, x: xForTimestamp(marker.t) }));
  const entryMarker = chartMarkers.find((marker) => marker.tone === "entry");
  const exitMarker = chartMarkers.find((marker) => marker.tone === "exit");
  const hasPositionRange = Boolean(entryMarker && exitMarker && exitMarker.x > entryMarker.x);
  const focusX = entryMarker?.x ?? null;

  return <svg className="market-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="服务端 OHLC K 线图">
    <g className="chart-grid" aria-hidden="true">
      {gridValues.map((ratio) => { const y = plotTop + ratio * plotHeight; return <line key={ratio} x1="8" x2={width - 8} y1={y} y2={y} className="chart-grid-line" />; })}
      {gridValues.slice(0, -1).map((ratio) => { const y = plotTop + ratio * plotHeight + 4; const value = max - ratio * range; return <text key={`axis-${ratio}`} x={width - 9} y={y} textAnchor="end" className="chart-axis-label">{value.toFixed(2)}</text>; })}
    </g>
    {hasPositionRange ? <>
      <rect x="8" y={plotTop} width={Math.max(0, entryMarker!.x - 8)} height={plotHeight} className="chart-position-before" />
      <rect x={entryMarker!.x} y={plotTop} width={Math.max(0, exitMarker!.x - entryMarker!.x)} height={plotHeight} className="chart-position-holding" />
      <rect x={exitMarker!.x} y={plotTop} width={Math.max(0, width - 8 - exitMarker!.x)} height={plotHeight} className="chart-position-after" />
    </> : focusX !== null && <>
      <rect x="8" y={plotTop} width={Math.max(0, focusX - 8)} height={plotHeight} className="chart-focus-before" />
      <rect x={focusX} y={plotTop} width={Math.max(0, width - 8 - focusX)} height={plotHeight} className="chart-focus-after" />
    </>}
    <line x1="8" y1={height - plotBottom} x2={width - 8} y2={height - plotBottom} className="chart-baseline" />
    {bars.map((bar, index) => {
      const x = 8 + index * step + step / 2;
      const open = scale(bar.o);
      const close = scale(bar.c);
      const high = scale(bar.h);
      const low = scale(bar.l);
      const top = Math.min(open, close);
      const body = Math.max(1.5, Math.abs(open - close));
      const positive = bar.c >= bar.o;
      const timestamp = formatTimestamp(bar.t);
      return <g key={`${bar.t}-${index}`} className={positive ? "candle-positive" : "candle-negative"}>
        <title>{`${timestamp} 开 ${bar.o} 高 ${bar.h} 低 ${bar.l} 收 ${bar.c}`}</title>
        <line x1={x} x2={x} y1={high} y2={low} className="candle-wick" />
        <rect x={x - candleWidth / 2} y={top} width={candleWidth} height={body} rx="1" className="candle-body" />
      </g>;
    })}
    {chartMarkers.map((marker) => <g key={`${marker.tone}-${marker.t}`}>
      <line x1={marker.x} x2={marker.x} y1={plotTop} y2={height - plotBottom} className={`chart-focus-line chart-position-line-${marker.tone}`} />
      <text x={Math.min(width - 12, Math.max(20, marker.x + 6))} y={plotTop + 12} className={`chart-focus-label chart-position-label-${marker.tone}`}>{marker.label}</text>
    </g>)}
    {timeIndexes.map((index) => <text key={`time-${index}`} x={8 + index * step + step / 2} y={height - 8} textAnchor="middle" className="chart-time-label">{formatClock(bars[index].t)}</text>)}
  </svg>;
}

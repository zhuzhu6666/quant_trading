import { useEffect, useRef } from "react";
import { createChart, ColorType, IChartApi, ISeriesApi, Time } from "lightweight-charts";
import { CHART_THEME } from "@/lib/theme";

export interface EquityPoint {
  t: number;
  v: number;
}

interface Props {
  points: EquityPoint[];
  height?: number;
  color?: string;
}

export function EquityCurve({ points, height = 240, color = CHART_THEME.volume }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Area"> | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      layout: { background: { type: ColorType.Solid, color: CHART_THEME.bg }, textColor: CHART_THEME.text },
      grid: { vertLines: { color: CHART_THEME.grid }, horzLines: { color: CHART_THEME.grid } },
      rightPriceScale: { borderColor: CHART_THEME.border },
      timeScale: { borderColor: CHART_THEME.border },
      height,
    });
    const series = chart.addAreaSeries({
      lineColor: color,
      topColor: color + "55",
      bottomColor: color + "00",
      lineWidth: 2,
    });
    chartRef.current = chart;
    seriesRef.current = series;
    return () => { chart.remove(); chartRef.current = null; };
  }, [height, color]);

  useEffect(() => {
    if (!seriesRef.current) return;
    seriesRef.current.setData(points.map((p) => ({ time: p.t as Time, value: p.v })));
  }, [points]);

  return <div ref={containerRef} />;
}

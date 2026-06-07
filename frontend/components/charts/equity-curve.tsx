"use client";
import { useEffect, useRef } from "react";
import { createChart, ColorType, IChartApi, ISeriesApi, Time } from "lightweight-charts";

export interface EquityPoint {
  t: number;  // unix seconds
  v: number;  // equity value
}

interface Props {
  points: EquityPoint[];
  height?: number;
  color?: string;
}

export function EquityCurve({ points, height = 240, color = "#58a6ff" }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Area"> | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      layout: { background: { type: ColorType.Solid, color: "#0d1117" }, textColor: "#c9d1d9" },
      grid: { vertLines: { color: "#21262d" }, horzLines: { color: "#21262d" } },
      rightPriceScale: { borderColor: "#30363d" },
      timeScale: { borderColor: "#30363d" },
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

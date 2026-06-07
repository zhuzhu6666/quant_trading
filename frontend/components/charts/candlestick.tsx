"use client";
import { useEffect, useRef } from "react";
import { createChart, ColorType, IChartApi, ISeriesApi, Time } from "lightweight-charts";

export interface CandleBar {
  t: number;  // unix seconds
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
}

interface Props {
  bars: CandleBar[];
  height?: number;
}

export function Candlestick({ bars, height = 480 }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volSeriesRef = useRef<ISeriesApi<"Histogram"> | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      layout: { background: { type: ColorType.Solid, color: "#0d1117" }, textColor: "#c9d1d9" },
      grid: { vertLines: { color: "#21262d" }, horzLines: { color: "#21262d" } },
      rightPriceScale: { borderColor: "#30363d" },
      timeScale: { borderColor: "#30363d", timeVisible: true, secondsVisible: false },
      crosshair: { mode: 1 },
      height,
    });
    const candle = chart.addCandlestickSeries({
      upColor: "#3fb950", downColor: "#f85149",
      borderUpColor: "#3fb950", borderDownColor: "#f85149",
      wickUpColor: "#3fb950", wickDownColor: "#f85149",
    });
    const vol = chart.addHistogramSeries({
      color: "#58a6ff",
      priceFormat: { type: "volume" },
      priceScaleId: "vol",
    });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
    chartRef.current = chart;
    candleSeriesRef.current = candle;
    volSeriesRef.current = vol;
    return () => { chart.remove(); chartRef.current = null; };
  }, [height]);

  useEffect(() => {
    if (!candleSeriesRef.current || !volSeriesRef.current) return;
    const candleData = bars.map((b) => ({ time: b.t as Time, open: b.o, high: b.h, low: b.l, close: b.c }));
    const volData = bars.map((b) => ({
      time: b.t as Time,
      value: b.v,
      color: b.c >= b.o ? "#3fb95055" : "#f8514955",
    }));
    candleSeriesRef.current.setData(candleData);
    volSeriesRef.current.setData(volData);
  }, [bars]);

  return <div ref={containerRef} />;
}

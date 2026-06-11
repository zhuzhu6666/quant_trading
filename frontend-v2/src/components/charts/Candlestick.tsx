import { useEffect, useRef } from "react";
import { createChart, ColorType, IChartApi, ISeriesApi, Time } from "lightweight-charts";
import { CHART_THEME } from "@/lib/theme";

export interface CandleBar {
  t: number;
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
      layout: { background: { type: ColorType.Solid, color: CHART_THEME.bg }, textColor: CHART_THEME.text },
      grid: { vertLines: { color: CHART_THEME.grid }, horzLines: { color: CHART_THEME.grid } },
      rightPriceScale: { borderColor: CHART_THEME.border },
      timeScale: { borderColor: CHART_THEME.border, timeVisible: true, secondsVisible: false },
      crosshair: { mode: 1 },
      height,
    });
    const candle = chart.addCandlestickSeries({
      upColor: CHART_THEME.up, downColor: CHART_THEME.down,
      borderUpColor: CHART_THEME.up, borderDownColor: CHART_THEME.down,
      wickUpColor: CHART_THEME.up, wickDownColor: CHART_THEME.down,
    });
    const vol = chart.addHistogramSeries({
      color: CHART_THEME.volume,
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
      color: b.c >= b.o ? CHART_THEME.volumeUp : CHART_THEME.volumeDown,
    }));
    candleSeriesRef.current.setData(candleData);
    volSeriesRef.current.setData(volData);
    if (bars.length > 0) chartRef.current?.timeScale().fitContent();
  }, [bars]);

  return <div ref={containerRef} />;
}

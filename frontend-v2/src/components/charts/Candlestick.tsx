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
  const lastTimeRef = useRef<number>(0);

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
    lastTimeRef.current = 0;
    return () => { chart.remove(); chartRef.current = null; };
  }, [height]);

  useEffect(() => {
    const candle = candleSeriesRef.current;
    const vol = volSeriesRef.current;
    if (!candle || !vol || bars.length === 0) return;

    const prevLast = lastTimeRef.current;
    const newLast = bars[bars.length - 1]?.t ?? 0;

    // 增量更新: 最后 bar 时间变了 → 只 update 最后一根 (保留其他不动)
    if (prevLast > 0 && prevLast === newLast) {
      const b = bars[bars.length - 1];
      candle.update({ time: b.t as Time, open: b.o, high: b.h, low: b.l, close: b.c });
      vol.update({ time: b.t as Time, value: b.v, color: b.c >= b.o ? CHART_THEME.volumeUp : CHART_THEME.volumeDown });
    } else {
      // 全量: 初次加载、时间范围变更、bar 数量变化
      const candleData = bars.map((b) => ({ time: b.t as Time, open: b.o, high: b.h, low: b.l, close: b.c }));
      const volData = bars.map((b) => ({
        time: b.t as Time,
        value: b.v,
        color: b.c >= b.o ? CHART_THEME.volumeUp : CHART_THEME.volumeDown,
      }));
      candle.setData(candleData);
      vol.setData(volData);
      if (bars.length > 0) chartRef.current?.timeScale().fitContent();
    }
    lastTimeRef.current = newLast;
  }, [bars]);

  return <div ref={containerRef} />;
}

"use client";
import ReactECharts from "echarts-for-react";

interface FactorMetrics {
  score: number;
  abs_ic: number;
  stability: number;
  decay: number;
  regime_consistency: number;
  independence: number;
}

interface Props {
  metrics: FactorMetrics;
  height?: number;
}

export function FactorHealthRadar({ metrics, height = 320 }: Props) {
  // Normalize all 5 dims to 0-1 (scores are already 0-100 but for radar we show as ratio)
  const option = {
    backgroundColor: "transparent",
    textStyle: { color: "#c9d1d9" },
    radar: {
      indicator: [
        { name: "abs_ic",    max: 0.1 },
        { name: "stability", max: 1.0 },
        { name: "decay",     max: 1.0 },
        { name: "regime",    max: 1.0 },
        { name: "indep",     max: 1.0 },
      ],
      splitArea: { areaStyle: { color: ["#161b22", "#0d1117"] } },
      axisLine: { lineStyle: { color: "#30363d" } },
      splitLine: { lineStyle: { color: "#30363d" } },
      name: { textStyle: { color: "#c9d1d9", fontSize: 12 } },
    },
    series: [{
      type: "radar",
      data: [{
        value: [
          Math.min(0.1, metrics.abs_ic),
          metrics.stability,
          metrics.decay,
          metrics.regime_consistency,
          metrics.independence,
        ],
        name: metrics.score.toFixed(1),
        lineStyle: { color: "#58a6ff" },
        areaStyle: { color: "rgba(88,166,255,0.2)" },
        itemStyle: { color: "#58a6ff" },
      }],
    }],
  };
  return <ReactECharts option={option} style={{ height }} notMerge={true} lazyUpdate={true} />;
}

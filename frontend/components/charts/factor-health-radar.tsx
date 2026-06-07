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
      // (audit v6-fix-2: backend 5-dim scores are 0-100 (factor_health.py:
      // _compute_components "0-100 score for each of 5 dims"), not raw
      // 0-0.1 IC. v5 audit mis-set the max to 0.1, which would clip the
      // radar at the 100-cap for any factor with mean_abs_ic >= 10.)
      indicator: [
        { name: "abs_ic",    max: 100 },
        { name: "stability", max: 100 },
        { name: "decay",     max: 100 },
        { name: "regime",    max: 100 },
        { name: "indep",     max: 100 },
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
          Number.isFinite(metrics.abs_ic) ? Math.min(100, metrics.abs_ic) : 0,
          Number.isFinite(metrics.stability) ? Math.min(100, metrics.stability) : 0,
          Number.isFinite(metrics.decay) ? Math.min(100, metrics.decay) : 0,
          Number.isFinite(metrics.regime_consistency) ? Math.min(100, metrics.regime_consistency) : 0,
          Number.isFinite(metrics.independence) ? Math.min(100, metrics.independence) : 0,
        ],
        // (audit v6-fix-1: score may be NaN for factors with insufficient data.)
        name: Number.isFinite(metrics.score) ? metrics.score.toFixed(1) : "--",
        lineStyle: { color: "#58a6ff" },
        areaStyle: { color: "rgba(88,166,255,0.2)" },
        itemStyle: { color: "#58a6ff" },
      }],
    }],
  };
  return <ReactECharts option={option} style={{ height }} notMerge={true} lazyUpdate={true} />;
}

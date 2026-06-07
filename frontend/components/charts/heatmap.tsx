"use client";
import ReactECharts from "echarts-for-react";

interface Props {
  data: number[][];  // 2D array of values
  xLabels: string[];
  yLabels: string[];
  height?: number;
}

export function Heatmap({ data, xLabels, yLabels, height = 320 }: Props) {
  const cells: [number, number, number][] = [];
  for (let y = 0; y < data.length; y++) {
    for (let x = 0; x < data[y].length; x++) {
      cells.push([x, y, data[y][x]]);
    }
  }
  const option = {
    backgroundColor: "transparent",
    textStyle: { color: "#c9d1d9" },
    tooltip: { position: "top" },
    grid: { left: 80, right: 20, top: 30, bottom: 60 },
    xAxis: { type: "category", data: xLabels, splitArea: { show: true }, axisLabel: { rotate: 45 } },
    yAxis: { type: "category", data: yLabels, splitArea: { show: true } },
    visualMap: { min: -1, max: 1, calculable: true, orient: "horizontal", left: "center", bottom: 0, inRange: { color: ["#f85149", "#0d1117", "#3fb950"] }, textStyle: { color: "#c9d1d9" } },
    series: [{ type: "heatmap", data: cells, label: { show: false }, emphasis: { itemStyle: { shadowBlur: 10, shadowColor: "rgba(0,0,0,0.5)" } } }],
  };
  return <ReactECharts option={option} style={{ height }} notMerge={true} />;
}

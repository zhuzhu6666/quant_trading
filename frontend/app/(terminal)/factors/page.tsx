"use client";
import { useEffect, useState } from "react";
import Link from "next/link";

interface Factor {
  // (audit v6-fix-2: backend /api/factor-health/latest returns
  // { factor, score, status, components: {mean_abs_ic, ic_stability,
  // regime_consistency, decay_rate, independence}, n_obs, rolling_ic }.
  // v5 audit misread this from README and the page crashed with
  // TypeError because every access was undefined. Realigned below.)
  factor: string;
  score: number;
  status: "HEALTHY" | "WATCH" | "DECAYING";
  components: {
    mean_abs_ic: number;
    ic_stability: number;
    regime_consistency: number;
    decay_rate: number;
    independence: number;
  };
  n_obs: number;
  rolling_ic: number;
}

// Flatten Factor.components into the table-friendly shape the UI expects.
// Backend nests the 5-dim 0-100 scores under .components; the table
// reads them flat. mean_abs_ic is itself a 0-100 score (see
// factor_health.py:_compute_components docstring "0-100 score for
// each of 5 dims"), NOT a raw IC value, so we pass it through as-is.
function flat(f: Factor) {
  return {
    name: f.factor,
    status: f.status,
    score: f.score,
    abs_ic: f.components.mean_abs_ic,
    stability: f.components.ic_stability,
    decay: f.components.decay_rate,
    regime_consistency: f.components.regime_consistency,
    independence: f.components.independence,
  };
}

export default function FactorsPage() {
  const [report, setReport] = useState<{ factors: Factor[]; healthy: number; watch: number; decaying: number } | null>(null);
  const [running, setRunning] = useState(false);

  async function load() {
    const r = await fetch("/api/factor-health/latest");
    const d = await r.json();
    if (d.report) setReport(d.report);
  }

  useEffect(() => { load(); }, []);

  async function run() {
    setRunning(true);
    try {
      await fetch("/api/factor-health/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ threshold: 0.04, bar_count: 50000, sync_run: false }),
      });
      // Poll latest after 30s
      setTimeout(load, 30000);
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">因子健康</h1>
      <div className="flex items-center gap-4">
        <button onClick={run} disabled={running} className="bg-accent text-bg font-semibold px-4 py-2 rounded disabled:opacity-50">
          {running ? "提交中..." : "▶ 重新评估"}
        </button>
        {report && (
          <div className="flex gap-4 text-sm">
            <span className="text-up">● {report.healthy} HEALTHY</span>
            <span className="text-warn">● {report.watch} WATCH</span>
            <span className="text-down">● {report.decaying} DECAYING</span>
          </div>
        )}
      </div>
      {report ? (
        <div className="bg-bg-card border border-bg-border rounded overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-fg-muted">
              <tr className="border-b border-bg-border">
                <th className="text-left p-2">名称</th>
                <th className="text-left p-2">状态</th>
                <th className="text-right p-2">得分</th>
                <th className="text-right p-2">abs IC</th>
                <th className="text-right p-2">stability</th>
                <th className="text-right p-2">regime</th>
              </tr>
            </thead>
            <tbody className="num">
              {report.factors.slice(0, 50).map((raw) => {
                const f = flat(raw);
                return (
                <tr key={f.name} className="border-b border-bg-border/50 hover:bg-bg-border/30">
                  <td className="p-2">
                    <Link href={`/factors/${encodeURIComponent(f.name)}`} className="text-accent hover:underline">{f.name}</Link>
                  </td>
                  <td className={`p-2 ${f.status === "HEALTHY" ? "text-up" : f.status === "WATCH" ? "text-warn" : "text-down"}`}>
                    {f.status}
                  </td>
                  {/* (audit v6-fix-1: f.score/abs_ic/stability/regime_consistency may be
                    undefined for factors with NaN/insufficient data. Guard with
                    Number.isFinite so the page doesn't crash on TypeError. Show "--"
                    matching the convention used by paper / topbar / format.ts.) */}
                  <td className="p-2 text-right">{Number.isFinite(f.score) ? f.score.toFixed(1) : "--"}</td>
                  <td className="p-2 text-right">{Number.isFinite(f.abs_ic) ? f.abs_ic.toFixed(4) : "--"}</td>
                  <td className="p-2 text-right">{Number.isFinite(f.stability) ? f.stability.toFixed(2) : "--"}</td>
                  <td className="p-2 text-right">{Number.isFinite(f.regime_consistency) ? f.regime_consistency.toFixed(2) : "--"}</td>
                </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="text-fg-muted">尚无报告,点击"重新评估"生成。</div>
      )}
    </div>
  );
}

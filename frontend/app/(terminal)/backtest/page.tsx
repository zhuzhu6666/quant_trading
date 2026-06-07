"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { fmtNum, fmtPct, fmtUSD } from "@/lib/format";

interface BacktestRequest {
  symbol: string;
  timeframe: string;
  risk_per_trade_pct: number | null;
  enable_circuit: boolean;
}

const DEFAULT_CONFIG: BacktestRequest = {
  symbol: "XAUUSD+",
  timeframe: "M15",
  risk_per_trade_pct: 1.0,
  enable_circuit: false,
};

interface JobState {
  id: string;
  status: "queued" | "running" | "done" | "error" | "cancelled";
  progress_pct: number;
  current_step: string;
  started_at: string;
  finished_at: string | null;
  result: { rows: any[]; total_runs: number; elapsed_seconds: number; report_path: string; note: string } | null;
  error: string | null;
}

export default function BacktestPage() {
  const [config, setConfig] = useState<BacktestRequest>(DEFAULT_CONFIG);
  const [jobId, setJobId] = useState<string | null>(null);
  const [job, setJob] = useState<JobState | null>(null);
  const [busy, setBusy] = useState(false);
  const [reportText, setReportText] = useState<string | null>(null);
  const [recentJobs, setRecentJobs] = useState<JobState[]>([]);

  async function loadRecent() {
    // (audit v7-fix-2: trailing slash hits Next.js dev 404 because the
    // FastAPI route is registered as /api/backtest (no slash). The HTML
    // response then chokes fetch's `r.json()`. Use canonical /api/backtest.)
    const r = await fetch("/api/backtest?status=done");
    if (!r.ok) return;
    const d = await r.json();
    setRecentJobs((d.jobs || []).slice(0, 5));
  }

  useEffect(() => { loadRecent(); }, []);

  async function start() {
    setBusy(true);
    setJob(null);
    setReportText(null);
    try {
      const r = await fetch("/api/backtest/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(config),
      });
      if (!r.ok) {
        alert(`提交失败: ${r.status} ${r.statusText}`);
        return;
      }
      const d = await r.json();
      setJobId(d.job_id);
      poll(d.job_id);
    } finally {
      setBusy(false);
    }
  }

  async function poll(id: string) {
    for (let i = 0; i < 180; i++) {  // up to 6 min
      await new Promise((r) => setTimeout(r, 2000));
      const r = await fetch(`/api/backtest/${id}`);
      if (!r.ok) break;
      const d: JobState = await r.json();
      setJob(d);
      if (d.status === "done" || d.status === "error" || d.status === "cancelled") {
        if (d.status === "done" && d.result?.report_path) {
          // Read the txt report from disk via the reports endpoint
          const name = d.result.report_path.split(/[\\/]/).pop()!;
          const rr = await fetch(`/api/reports/${encodeURIComponent(name)}`);
          if (rr.ok) {
            const dd = await rr.json();
            setReportText(typeof dd.content === "string" ? dd.content : JSON.stringify(dd.content, null, 2));
          }
        }
        loadRecent();
        return;
      }
    }
  }

  const rows = job?.result?.rows ?? [];
  const best = rows.length > 0
    ? rows
        .filter((r: any) => r.net_pnl !== undefined)
        .reduce((a: any, b: any) => (b.net_pnl > (a?.net_pnl ?? -Infinity) ? b : a), null)
    : null;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">回测</h1>
        <Link href="/jobs" className="text-xs text-fg-muted hover:text-fg">查看任务中心 →</Link>
      </div>

      <div className="bg-warn/10 border border-warn rounded p-3 text-sm text-warn">
        ⚠ 当前 <code>backend/services/backtest_runner.py</code> 的 12 combo 走 stub 路径 (Phase 4.7+ 待实装 backtrader optstrategy)。
        Web 端跑出来 12 行 trades=0/PnL=0/报告头部带 <code># NOTE: in-process stub</code>。
        真实 PnL 仍需 <code>python main.py --mode backtest</code>。详见 <code>PROJECT_AUDIT_v5.md</code> B-2。
      </div>

      <div className="bg-bg-card border border-bg-border rounded p-4 grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
        <div>
          <label className="text-fg-muted text-xs">symbol</label>
          <input value={config.symbol} onChange={(e) => setConfig({ ...config, symbol: e.target.value })} className="w-full bg-bg border border-bg-border rounded px-2 py-1" />
        </div>
        <div>
          <label className="text-fg-muted text-xs">timeframe</label>
          <select value={config.timeframe} onChange={(e) => setConfig({ ...config, timeframe: e.target.value })} className="w-full bg-bg border border-bg-border rounded px-2 py-1">
            <option>M5</option><option>M15</option><option>M30</option>
            <option>H1</option><option>H4</option><option>D1</option>
          </select>
        </div>
        <div>
          <label className="text-fg-muted text-xs">risk_per_trade_pct</label>
          <input type="number" step="0.1" value={config.risk_per_trade_pct ?? ""} onChange={(e) => setConfig({ ...config, risk_per_trade_pct: e.target.value === "" ? null : parseFloat(e.target.value) })} className="w-full bg-bg border border-bg-border rounded px-2 py-1 num" />
        </div>
        <div className="flex items-end">
          <label className="flex items-center gap-2 cursor-pointer">
            <input type="checkbox" checked={config.enable_circuit} onChange={(e) => setConfig({ ...config, enable_circuit: e.target.checked })} />
            <span className="text-sm">启用熔断</span>
          </label>
        </div>
      </div>

      <div className="flex gap-2">
        <button onClick={start} disabled={busy || job?.status === "running" || job?.status === "queued"} className="bg-accent text-bg font-semibold px-4 py-2 rounded disabled:opacity-50">
          {busy ? "提交中..." : "▶ 跑一次回测"}
        </button>
        {jobId && <div className="text-xs text-fg-muted self-center">job: {jobId}</div>}
      </div>

      {job && (
        <div className="bg-bg-card border border-bg-border rounded p-4 space-y-2">
          <div className="flex items-center justify-between text-sm">
            <span className="text-fg-muted">{job.status}</span>
            <span className="num">{job.progress_pct.toFixed(0)}%</span>
          </div>
          <div className="h-2 bg-bg-border rounded overflow-hidden">
            <div className="h-full bg-accent transition-all" style={{ width: `${job.progress_pct}%` }} />
          </div>
          <div className="text-xs text-fg-muted">{job.current_step}</div>
          {job.error && <div className="text-down text-xs">{job.error}</div>}
        </div>
      )}

      {best && (
        <div className="bg-bg-card border border-bg-border rounded p-4">
          <div className="text-fg-muted text-xs mb-2">最优组合 (12 combo 之中)</div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
            <div><div className="text-fg-muted text-xs">sl_atr</div><div className="num">{best.sl_atr}</div></div>
            <div><div className="text-fg-muted text-xs">tp_atr</div><div className="num">{best.tp_atr}</div></div>
            <div><div className="text-fg-muted text-xs">cooldown</div><div className="num">{best.cooldown_bars}</div></div>
            <div><div className="text-fg-muted text-xs">trades</div><div className="num">{best.trades}</div></div>
            <div><div className="text-fg-muted text-xs">net PnL</div><div className={`num ${best.net_pnl >= 0 ? "text-up" : "text-down"}`}>{fmtNum(best.net_pnl)}</div></div>
            <div><div className="text-fg-muted text-xs">Sharpe</div><div className="num">{fmtNum(best.sharpe)}</div></div>
            <div><div className="text-fg-muted text-xs">MaxDD</div><div className="num">{fmtPct(best.max_drawdown)}</div></div>
            <div><div className="text-fg-muted text-xs">WR</div><div className="num">{fmtPct(best.win_rate * 100)}</div></div>
          </div>
        </div>
      )}

      {rows.length > 0 && (
        <div className="bg-bg-card border border-bg-border rounded overflow-x-auto">
          <table className="w-full text-sm num">
            <thead className="text-fg-muted">
              <tr className="border-b border-bg-border">
                <th className="text-right p-2">sl</th>
                <th className="text-right p-2">tp</th>
                <th className="text-right p-2">cd</th>
                <th className="text-right p-2">trades</th>
                <th className="text-right p-2">WR</th>
                <th className="text-right p-2">net PnL</th>
                <th className="text-right p-2">ret%</th>
                <th className="text-right p-2">Sharpe</th>
                <th className="text-right p-2">MaxDD</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r: any, i: number) => (
                <tr key={i} className="border-b border-bg-border/50">
                  <td className="p-2 text-right">{r.sl_atr}</td>
                  <td className="p-2 text-right">{r.tp_atr}</td>
                  <td className="p-2 text-right">{r.cooldown_bars}</td>
                  <td className="p-2 text-right">{r.trades ?? "--"}</td>
                  <td className="p-2 text-right">{r.win_rate !== undefined ? fmtPct(r.win_rate * 100) : "--"}</td>
                  <td className={`p-2 text-right ${(r.net_pnl ?? 0) >= 0 ? "text-up" : "text-down"}`}>{r.net_pnl !== undefined ? fmtUSD(r.net_pnl) : "--"}</td>
                  <td className="p-2 text-right">{r.total_return !== undefined ? fmtPct(r.total_return) : "--"}</td>
                  <td className="p-2 text-right">{r.sharpe !== undefined ? fmtNum(r.sharpe) : "--"}</td>
                  <td className="p-2 text-right">{r.max_drawdown !== undefined ? fmtPct(r.max_drawdown) : "--"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {reportText && (
        <div className="bg-bg-card border border-bg-border rounded p-4">
          <div className="text-fg-muted text-xs mb-2">报告原文 (data/charts/backtest_*.txt)</div>
          <pre className="text-xs whitespace-pre-wrap num text-fg">{reportText}</pre>
        </div>
      )}

      {recentJobs.length > 0 && (
        <div className="bg-bg-card border border-bg-border rounded p-4">
          <div className="text-fg-muted text-xs mb-2">最近 5 次回测</div>
          <div className="space-y-1 text-xs num">
            {recentJobs.map((j) => (
              <div key={j.id} className="flex items-center justify-between text-fg-muted">
                <span className="font-mono">{j.id.slice(0, 8)}</span>
                <span>{j.started_at.slice(0, 19)}</span>
                <span>{j.result?.total_runs ?? 0} combos · {(j.result?.elapsed_seconds ?? 0).toFixed(1)}s</span>
                <span className="text-fg">{j.status}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

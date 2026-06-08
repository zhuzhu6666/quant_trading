"use client";
import { useState } from "react";
import { authFetch } from "@/lib/auth";

interface TopFactor { name: string; expr: string; ic: number; }

export default function DiscoverPage() {
  const [engine, setEngine] = useState<"gp" | "random">("gp");
  const [nCandidates, setNCandidates] = useState(1000);
  const [topK, setTopK] = useState(50);
  const [forwardPeriods, setForwardPeriods] = useState("1,5,20");
  const [autoRegister, setAutoRegister] = useState(true);
  const [gpPop, setGpPop] = useState(100);
  const [gpGen, setGpGen] = useState(20);
  const [jobId, setJobId] = useState<string | null>(null);
  const [progress, setProgress] = useState<{ pct: number; step: string; status: string } | null>(null);
  const [top, setTop] = useState<TopFactor[]>([]);

  async function start() {
    setProgress({ pct: 0, step: "提交中...", status: "queued" });
    setTop([]);
    const r = await authFetch("/api/discover", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        engine,
        n_candidates: nCandidates,
        top_k: topK,
        forward_periods: forwardPeriods.split(",").map((s) => parseInt(s.trim())).filter((n) => !isNaN(n)),
        auto_register: autoRegister,
        gp_pop: gpPop,
        gp_gen: gpGen,
      }),
    });
    const d = await r.json();
    setJobId(d.job_id);
    poll(d.job_id);
  }

  async function poll(id: string) {
    for (let i = 0; i < 600; i++) {  // up to 20 min
      await new Promise((r) => setTimeout(r, 2000));
      const r = await authFetch(`/api/discover/${id}`);
      if (!r.ok) break;
      const d = await r.json();
      setProgress({ pct: d.progress_pct, step: d.current_step, status: d.status });
      if (d.status === "done") { setTop(d.top_factors || []); return; }
      if (d.status === "error" || d.status === "cancelled") return;
    }
  }

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">L2 因子发现</h1>
      <div className="bg-bg-card border border-bg-border rounded p-4 grid grid-cols-1 md:grid-cols-3 gap-3 text-sm">
        <div className="md:col-span-3 flex gap-4">
          <label className="flex items-center gap-2 cursor-pointer">
            <input type="radio" name="engine" checked={engine === "gp"} onChange={() => setEngine("gp")} />
            <span>GP (推荐)</span>
          </label>
          <label className="flex items-center gap-2 cursor-pointer">
            <input type="radio" name="engine" checked={engine === "random"} onChange={() => setEngine("random")} />
            <span>Random</span>
          </label>
        </div>
        <div>
          <label className="text-fg-muted text-xs">n_candidates</label>
          <input type="number" value={nCandidates} onChange={(e) => setNCandidates(parseInt(e.target.value) || 0)} className="w-full bg-bg border border-bg-border rounded px-2 py-1 num" />
        </div>
        <div>
          <label className="text-fg-muted text-xs">top_k</label>
          <input type="number" value={topK} onChange={(e) => setTopK(parseInt(e.target.value) || 0)} className="w-full bg-bg border border-bg-border rounded px-2 py-1 num" />
        </div>
        <div>
          <label className="text-fg-muted text-xs">forward_periods (csv)</label>
          <input type="text" value={forwardPeriods} onChange={(e) => setForwardPeriods(e.target.value)} className="w-full bg-bg border border-bg-border rounded px-2 py-1" />
        </div>
        {engine === "gp" && (
          <>
            <div>
              <label className="text-fg-muted text-xs">gp_pop</label>
              <input type="number" value={gpPop} onChange={(e) => setGpPop(parseInt(e.target.value) || 0)} className="w-full bg-bg border border-bg-border rounded px-2 py-1 num" />
            </div>
            <div>
              <label className="text-fg-muted text-xs">gp_gen</label>
              <input type="number" value={gpGen} onChange={(e) => setGpGen(parseInt(e.target.value) || 0)} className="w-full bg-bg border border-bg-border rounded px-2 py-1 num" />
            </div>
          </>
        )}
        <div className="md:col-span-3 flex items-center gap-2 pt-2">
          <input type="checkbox" id="autoreg" checked={autoRegister} onChange={(e) => setAutoRegister(e.target.checked)} />
          <label htmlFor="autoreg" className="cursor-pointer">自动注册为 shadow 因子</label>
        </div>
      </div>
      <div className="flex gap-2">
        <button onClick={start} disabled={progress?.status === "running" || progress?.status === "queued"} className="bg-accent text-bg font-semibold px-4 py-2 rounded disabled:opacity-50">
          ▶ 开始发现
        </button>
        {jobId && <div className="text-xs text-fg-muted self-center">job: {jobId}</div>}
      </div>
      {progress && (
        <div className="bg-bg-card border border-bg-border rounded p-4 space-y-2">
          <div className="flex items-center justify-between text-sm">
            <span className="text-fg-muted">{progress.status}</span>
            <span className="num">{progress.pct.toFixed(0)}%</span>
          </div>
          <div className="h-2 bg-bg-border rounded overflow-hidden">
            <div className="h-full bg-accent transition-all" style={{ width: `${progress.pct}%` }} />
          </div>
          <div className="text-xs text-fg-muted">{progress.step}</div>
        </div>
      )}
      {top.length > 0 && (
        <div className="bg-bg-card border border-bg-border rounded overflow-x-auto">
          <table className="w-full text-sm num">
            <thead className="text-fg-muted">
              <tr className="border-b border-bg-border">
                <th className="text-left p-2">#</th>
                <th className="text-left p-2">name</th>
                <th className="text-right p-2">IC</th>
                <th className="text-left p-2">expr</th>
              </tr>
            </thead>
            <tbody>
              {top.map((f, i) => (
                <tr key={f.name} className="border-b border-bg-border/50">
                  <td className="p-2 text-fg-muted">{i + 1}</td>
                  <td className="p-2 text-fg">{f.name}</td>
                  <td className="p-2 text-right">{f.ic.toFixed(4)}</td>
                  <td className="p-2 text-fg-muted truncate max-w-md">{f.expr}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

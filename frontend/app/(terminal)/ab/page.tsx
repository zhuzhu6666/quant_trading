"use client";
import { useState } from "react";

export default function ABPage() {
  const [pathA, setPathA] = useState("baseline");
  const [pathB, setPathB] = useState("reverse");
  const [nBars, setNBars] = useState(5000);
  const [jobId, setJobId] = useState<string | null>(null);
  const [progress, setProgress] = useState<{ pct: number; step: string; status: string } | null>(null);
  const [report, setReport] = useState<string | null>(null);

  async function start() {
    setProgress({ pct: 0, step: "提交中...", status: "queued" });
    setReport(null);
    const r = await fetch("/api/ab/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path_a: pathA, path_b: pathB, n_bars: nBars }),
    });
    const d = await r.json();
    setJobId(d.job_id);
    poll(d.job_id);
  }

  async function poll(id: string) {
    for (let i = 0; i < 300; i++) {  // up to 10 min
      await new Promise((r) => setTimeout(r, 2000));
      const r = await fetch(`/api/ab/${id}`);
      if (!r.ok) break;
      const d = await r.json();
      setProgress({ pct: d.progress_pct, step: d.current_step, status: d.status });
      if (d.status === "done") {
        setReport(d.result?.report_excerpt ?? "(no excerpt)");
        return;
      }
      if (d.status === "error" || d.status === "cancelled") return;
    }
  }

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">A/B 测试</h1>
      <div className="bg-bg-card border border-bg-border rounded p-4 grid grid-cols-1 md:grid-cols-3 gap-3 text-sm">
        <div>
          <label className="text-fg-muted text-xs">path A</label>
          <select value={pathA} onChange={(e) => setPathA(e.target.value)} className="w-full bg-bg border border-bg-border rounded px-2 py-1">
            <option value="baseline">baseline</option>
            <option value="uniform">uniform</option>
            <option value="reverse">reverse</option>
          </select>
        </div>
        <div>
          <label className="text-fg-muted text-xs">path B</label>
          <select value={pathB} onChange={(e) => setPathB(e.target.value)} className="w-full bg-bg border border-bg-border rounded px-2 py-1">
            <option value="baseline">baseline</option>
            <option value="uniform">uniform</option>
            <option value="reverse">reverse</option>
          </select>
        </div>
        <div>
          <label className="text-fg-muted text-xs">n_bars</label>
          <input type="number" value={nBars} onChange={(e) => setNBars(parseInt(e.target.value) || 0)} className="w-full bg-bg border border-bg-border rounded px-2 py-1 num" />
        </div>
      </div>
      <div className="flex gap-2">
        <button onClick={start} disabled={progress?.status === "running" || progress?.status === "queued"} className="bg-accent text-bg font-semibold px-4 py-2 rounded disabled:opacity-50">
          ▶ 开始 A/B
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
      {report && (
        <div className="bg-bg-card border border-bg-border rounded p-4">
          <div className="text-fg-muted text-xs mb-2">报告 (末 2000 字符)</div>
          <pre className="text-xs whitespace-pre-wrap num text-fg">{report}</pre>
        </div>
      )}
    </div>
  );
}

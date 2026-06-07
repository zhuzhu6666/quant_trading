"use client";
import { useEffect, useState } from "react";

interface Job {
  id: string;
  kind: string;
  status: "queued" | "running" | "done" | "error" | "cancelled";
  progress_pct: number;
  current_step: string;
  started_at: string;
  finished_at: string | null;
  params: Record<string, any>;
  result: any;
  error: string | null;
}

const KINDS = ["all", "backtest", "factor_health", "discover", "sync", "tuning", "ab_test"];
const STATUSES = ["all", "queued", "running", "done", "error", "cancelled"];

export default function JobsPage() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [kindFilter, setKindFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState("all");
  const [selected, setSelected] = useState<Job | null>(null);

  async function load() {
    const params = new URLSearchParams();
    if (kindFilter !== "all") params.set("kind", kindFilter);
    if (statusFilter !== "all") params.set("status", statusFilter);
    const r = await fetch(`/api/jobs?${params}`);
    const d = await r.json();
    setJobs(d.jobs || []);
  }

  useEffect(() => { load(); }, [kindFilter, statusFilter]);
  useEffect(() => {
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [kindFilter, statusFilter]);

  async function cancel(id: string) {
    await fetch(`/api/jobs/${id}/cancel`, { method: "POST" });
    await load();
  }

  async function select(id: string) {
    const r = await fetch(`/api/jobs/${id}`);
    if (r.ok) setSelected(await r.json());
  }

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">任务中心</h1>
      <div className="flex items-center gap-4 text-sm">
        <label>
          类型:
          <select value={kindFilter} onChange={(e) => setKindFilter(e.target.value)} className="ml-1 bg-bg border border-bg-border rounded px-2 py-1">
            {KINDS.map((k) => <option key={k}>{k}</option>)}
          </select>
        </label>
        <label>
          状态:
          <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} className="ml-1 bg-bg border border-bg-border rounded px-2 py-1">
            {STATUSES.map((s) => <option key={s}>{s}</option>)}
          </select>
        </label>
        <button onClick={load} className="text-xs text-fg-muted hover:text-fg">刷新</button>
        <span className="text-xs text-fg-muted ml-auto">每 5s 自动刷新</span>
      </div>

      <div className="bg-bg-card border border-bg-border rounded overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-fg-muted">
            <tr className="border-b border-bg-border">
              <th className="text-left p-2">id</th>
              <th className="text-left p-2">kind</th>
              <th className="text-left p-2">status</th>
              <th className="text-right p-2">progress</th>
              <th className="text-left p-2">step</th>
              <th className="text-right p-2">started</th>
              <th className="text-right p-2">操作</th>
            </tr>
          </thead>
          <tbody className="num">
            {jobs.length === 0 ? (
              <tr><td colSpan={7} className="p-4 text-fg-muted text-center">无任务</td></tr>
            ) : jobs.map((j) => (
              <tr key={j.id} onClick={() => select(j.id)} className={`border-b border-bg-border/50 cursor-pointer hover:bg-bg-border/30 ${selected?.id === j.id ? "bg-accent/10" : ""}`}>
                <td className="p-2 text-fg-muted font-mono text-xs">{j.id.slice(0, 8)}</td>
                <td className="p-2 text-fg">{j.kind}</td>
                <td className={`p-2 ${j.status === "running" ? "text-accent" : j.status === "done" ? "text-up" : j.status === "error" ? "text-down" : "text-fg-muted"}`}>
                  {j.status}
                </td>
                <td className="p-2 text-right">{j.progress_pct.toFixed(0)}%</td>
                <td className="p-2 text-fg-muted truncate max-w-xs">{j.current_step}</td>
                <td className="p-2 text-right text-fg-muted">{j.started_at.slice(11, 19)}</td>
                <td className="p-2 text-right">
                  {j.status === "running" && (
                    <button onClick={(e) => { e.stopPropagation(); cancel(j.id); }} className="text-xs bg-warn/20 text-warn px-2 py-1 rounded">cancel</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {selected && (
        <div className="bg-bg-card border border-bg-border rounded p-4 space-y-2">
          <div className="flex items-center justify-between">
            <div className="text-fg-muted text-sm">任务详情: <span className="text-fg font-mono">{selected.id}</span></div>
            <button onClick={() => setSelected(null)} className="text-xs text-fg-muted hover:text-fg">关闭</button>
          </div>
          <pre className="text-xs whitespace-pre-wrap num text-fg">{JSON.stringify(selected, null, 2)}</pre>
        </div>
      )}
    </div>
  );
}

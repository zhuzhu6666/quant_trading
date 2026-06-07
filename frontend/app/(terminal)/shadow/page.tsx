"use client";
import { useEffect, useState } from "react";

interface Shadow {
  name: string;
  status?: string;
  action?: string;
  ts?: string;
  expr?: string;
  ic?: number;
  cv_score?: number;
}

export default function ShadowPage() {
  const [shadows, setShadows] = useState<Shadow[]>([]);
  const [busy, setBusy] = useState<string | null>(null);

  async function load() {
    const r = await fetch("/api/shadow");
    const d = await r.json();
    setShadows(d.shadows || []);
  }

  useEffect(() => { load(); }, []);

  async function act(name: string, action: "promote" | "demote") {
    setBusy(name);
    try {
      await fetch(`/api/shadow/${action}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      await load();
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">影子因子</h1>
        <button onClick={load} className="text-xs text-fg-muted hover:text-fg">刷新</button>
      </div>
      <div className="bg-bg-card border border-bg-border rounded overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-fg-muted">
            <tr className="border-b border-bg-border">
              <th className="text-left p-2">name</th>
              <th className="text-left p-2">status</th>
              <th className="text-left p-2">last action</th>
              <th className="text-right p-2">IC</th>
              <th className="text-right p-2">CV</th>
              <th className="text-right p-2">ts</th>
              <th className="text-right p-2">操作</th>
            </tr>
          </thead>
          <tbody className="num">
            {shadows.length === 0 ? (
              <tr><td colSpan={7} className="p-4 text-fg-muted text-center">尚无影子因子</td></tr>
            ) : shadows.map((s) => (
              <tr key={s.name} className="border-b border-bg-border/50">
                <td className="p-2 text-fg">{s.name}</td>
                <td className={`p-2 ${s.status === "active" ? "text-up" : "text-warn"}`}>{s.status ?? "--"}</td>
                <td className="p-2 text-fg-muted">{s.action ?? "--"}</td>
                <td className="p-2 text-right">{(s.ic ?? 0).toFixed(4)}</td>
                <td className="p-2 text-right">{(s.cv_score ?? 0).toFixed(3)}</td>
                <td className="p-2 text-right text-fg-muted">{s.ts ?? "--"}</td>
                <td className="p-2 text-right space-x-1">
                  <button onClick={() => act(s.name, "promote")} disabled={busy === s.name} className="text-xs bg-up/20 text-up px-2 py-1 rounded disabled:opacity-50">promote</button>
                  <button onClick={() => act(s.name, "demote")} disabled={busy === s.name} className="text-xs bg-warn/20 text-warn px-2 py-1 rounded disabled:opacity-50">demote</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

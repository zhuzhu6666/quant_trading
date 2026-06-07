"use client";
import { useEffect, useState } from "react";

interface PerTF { M5?: { last_sync_utc: string; total_bars: number }; M15?: { last_sync_utc: string; total_bars: number }; H1?: { last_sync_utc: string; total_bars: number }; D1?: { last_sync_utc: string; total_bars: number }; }

export default function SyncPage() {
  const [status, setStatus] = useState<{ per_tf: PerTF; daemon_running: boolean } | null>(null);
  const [running, setRunning] = useState(false);

  async function load() {
    const r = await fetch("/api/sync/status");
    setStatus(await r.json());
  }
  useEffect(() => { load(); }, []);

  async function runOnce() {
    setRunning(true);
    try {
      await fetch("/api/sync/once", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ timeframes: ["M15", "H1", "D1"], type: "incremental" }),
      });
      setTimeout(load, 5000);
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">T16 实时数据同步</h1>
      <div className="bg-bg-card border border-bg-border rounded p-4">
        <div className="text-warn text-sm mb-2">⚠ T16 当前暂停 (2026-06-03): Python MetaTrader5 5.0.5735 vs MT5 terminal 2026 IPC pipe hash 不匹配,包 WaitNamedPipeW 一直 timeout。回退命令: <code>python scripts/live_sync.py --mode once --type incremental --timeframes M15,H1,D1</code></div>
      </div>
      <div className="flex gap-2">
        <button onClick={runOnce} disabled={running} className="bg-accent text-bg font-semibold px-4 py-2 rounded disabled:opacity-50">
          {running ? "提交中..." : "▶ 触发一次同步"}
        </button>
        <button onClick={load} className="bg-bg-card border border-bg-border px-4 py-2 rounded">刷新</button>
      </div>
      {status?.per_tf && (
        <div className="bg-bg-card border border-bg-border rounded overflow-x-auto">
          <table className="w-full text-sm num">
            <thead className="text-fg-muted">
              <tr className="border-b border-bg-border">
                <th className="text-left p-2">Timeframe</th>
                <th className="text-right p-2">Bars</th>
                <th className="text-right p-2">Last sync</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(status.per_tf).map(([tf, info]) => (
                <tr key={tf} className="border-b border-bg-border/50">
                  <td className="p-2 text-fg">{tf}</td>
                  <td className="p-2 text-right">{(info as any)?.total_bars ?? "--"}</td>
                  <td className="p-2 text-right text-fg-muted">{(info as any)?.last_sync_utc ?? "--"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

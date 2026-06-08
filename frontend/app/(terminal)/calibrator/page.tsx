"use client";
import { useEffect, useState } from "react";
import { authFetch } from "@/lib/auth";

interface CalibratorStatus {
  path: string;
  exists: boolean;
  buckets: Array<{ bin: number; raw: number; calibrated: number; n: number }> | null;
  platt?: { a: number; b: number };
  last_modified?: string;
  error?: string;
}

export default function CalibratorPage() {
  const [status, setStatus] = useState<CalibratorStatus | null>(null);
  const [editing, setEditing] = useState<string>("");
  const [saved, setSaved] = useState<string | null>(null);

  async function load() {
    const r = await authFetch("/api/calibrator");
    const d = await r.json();
    setStatus(d);
    setEditing(JSON.stringify(d.buckets ?? [], null, 2));
  }

  useEffect(() => { load(); }, []);

  async function saveBuckets() {
    try {
      const buckets = JSON.parse(editing);
      const r = await authFetch("/api/calibrator/save", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ buckets }),
      });
      const d = await r.json();
      setSaved(`保存 ${d.saved_n} 桶到 ${d.path}`);
      await load();
    } catch (e: any) {
      setSaved(`解析/保存失败: ${e.message}`);
    }
  }

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">概率校准器</h1>
      <div className="bg-bg-card border border-bg-border rounded p-4 text-sm space-y-2">
        <div className="text-fg-muted">路径</div>
        <div className="text-fg-muted font-mono text-xs">{status?.path ?? "--"}</div>
        {status && (
          <div className="flex gap-4 text-xs">
            <span>存在: <span className={status.exists ? "text-up" : "text-down"}>{status.exists ? "是" : "否"}</span></span>
            {status.last_modified && <span>修改: <span className="num text-fg-muted">{status.last_modified}</span></span>}
            {status.platt && status.platt.a != null && status.platt.b != null && (
              <span>Platt: <span className="num">a={Number.isFinite(status.platt.a) ? status.platt.a.toFixed(3) : "--"} b={Number.isFinite(status.platt.b) ? status.platt.b.toFixed(3) : "--"}</span></span>
            )}
          </div>
        )}
      </div>
      {status?.buckets && (
        <div className="bg-bg-card border border-bg-border rounded overflow-x-auto">
          <table className="w-full text-sm num">
            <thead className="text-fg-muted">
              <tr className="border-b border-bg-border">
                <th className="text-right p-2">bin</th>
                <th className="text-right p-2">raw</th>
                <th className="text-right p-2">calibrated</th>
                <th className="text-right p-2">n</th>
              </tr>
            </thead>
            <tbody>
              {/* (audit v7-fix-3: backend /api/calibrator returns buckets as
                3-tuples [low, high, calibrated_value] (NOT objects with
                .bin / .raw / .calibrated / .n keys as v5 audit assumed).
                See calibrator_service / probability_calibrator.py. Adapt
                on the fly: derive bin label from low/high, calibrated
                from index 2, and "n" is not stored so we show "--".) */}
              {status.buckets.map((b, i) => {
                const low = Array.isArray(b) ? b[0] : (b as any).low;
                const high = Array.isArray(b) ? b[1] : (b as any).high;
                const cal = Array.isArray(b) ? b[2] : (b as any).calibrated;
                return (
                <tr key={i} className="border-b border-bg-border/50">
                  <td className="p-2 text-right">{Number.isFinite(low) && Number.isFinite(high) ? `${low.toFixed(2)}–${high.toFixed(2)}` : "--"}</td>
                  <td className="p-2 text-right">{Number.isFinite(low) ? low.toFixed(3) : "--"}</td>
                  <td className="p-2 text-right">{Number.isFinite(cal) ? cal.toFixed(3) : "--"}</td>
                  <td className="p-2 text-right text-fg-muted">--</td>
                </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <div className="bg-bg-card border border-bg-border rounded p-4 space-y-2">
        <div className="text-fg-muted text-xs">编辑 buckets JSON (高级)</div>
        <textarea
          value={editing}
          onChange={(e) => setEditing(e.target.value)}
          rows={10}
          className="w-full bg-bg border border-bg-border rounded p-2 font-mono text-xs num"
        />
        <div className="flex gap-2">
          <button onClick={saveBuckets} className="bg-accent text-bg font-semibold px-4 py-2 rounded">保存</button>
          <button onClick={load} className="bg-bg-border text-fg px-4 py-2 rounded">重载</button>
          {saved && <span className="text-xs text-fg-muted self-center">{saved}</span>}
        </div>
      </div>
    </div>
  );
}

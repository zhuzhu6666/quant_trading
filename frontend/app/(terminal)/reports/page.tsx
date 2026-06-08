"use client";
import { useEffect, useState } from "react";
import { authFetch } from "@/lib/auth";

interface ReportEntry {
  name: string;
  path: string;
  kind: string;
  size: number;
  modified_at: string;
}

export default function ReportsPage() {
  const [reports, setReports] = useState<ReportEntry[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [content, setContent] = useState<any>(null);
  const [truncated, setTruncated] = useState(false);
  const [filter, setFilter] = useState("all");

  async function load() {
    const r = await authFetch(`/api/reports?kind=${filter}`);
    const d = await r.json();
    setReports(d.reports || []);
  }
  useEffect(() => { load(); }, [filter]);

  async function open(name: string) {
    setSelected(name);
    setContent(null);
    setTruncated(false);
    const r = await authFetch(`/api/reports/${encodeURIComponent(name)}`);
    if (r.ok) {
      const d = await r.json();
      setContent(d.content);
      setTruncated(d.truncated ?? false);
    } else {
      setContent({ error: "read failed" });
    }
  }

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">报告浏览器</h1>
      <div className="flex items-center gap-4">
        <span className="text-sm text-fg-muted">{reports.length} 个文件</span>
        <select value={filter} onChange={(e) => setFilter(e.target.value)} className="bg-bg border border-bg-border rounded px-2 py-1 text-sm">
          <option value="all">all</option>
          <option value="txt">txt</option>
          <option value="json">json</option>
          <option value="png">png</option>
          <option value="npy">npy</option>
        </select>
        <button onClick={load} className="text-xs text-fg-muted hover:text-fg">刷新</button>
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="bg-bg-card border border-bg-border rounded overflow-x-auto max-h-[70vh] overflow-y-auto">
          <table className="w-full text-sm num">
            <thead className="text-fg-muted sticky top-0 bg-bg-card">
              <tr className="border-b border-bg-border">
                <th className="text-left p-2">name</th>
                <th className="text-left p-2">kind</th>
                <th className="text-right p-2">size</th>
                <th className="text-right p-2">modified</th>
              </tr>
            </thead>
            <tbody>
              {reports.map((r) => (
                <tr key={r.name} onClick={() => open(r.name)} className={`border-b border-bg-border/50 cursor-pointer hover:bg-bg-border/30 ${selected === r.name ? "bg-accent/10" : ""}`}>
                  <td className="p-2 text-fg">{r.name}</td>
                  <td className="p-2 text-fg-muted">{r.kind}</td>
                  <td className="p-2 text-right text-fg-muted">{(r.size / 1024).toFixed(1)}K</td>
                  <td className="p-2 text-right text-fg-muted">{r.modified_at.slice(0, 19)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="bg-bg-card border border-bg-border rounded p-4 max-h-[70vh] overflow-y-auto">
          {!selected && <div className="text-fg-muted">点击左侧文件名查看内容</div>}
          {selected && content !== null && (
            <div>
              <div className="flex items-center justify-between mb-2">
                <div className="text-sm text-fg-muted">{selected}</div>
                <button onClick={() => setSelected(null)} className="text-xs text-fg-muted hover:text-fg">关闭</button>
              </div>
              {truncated && <div className="text-warn text-xs mb-2">⚠ 文件 &gt; 1MB,已截断到末 1MB</div>}
              {typeof content === "string" && content.startsWith("data:image/png;base64,") ? (
                // @ts-ignore
                <img src={content} alt={selected} className="max-w-full" />
              ) : typeof content === "string" ? (
                <pre className="text-xs whitespace-pre-wrap num text-fg">{content}</pre>
              ) : (
                <pre className="text-xs whitespace-pre-wrap num text-fg">{JSON.stringify(content, null, 2)}</pre>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

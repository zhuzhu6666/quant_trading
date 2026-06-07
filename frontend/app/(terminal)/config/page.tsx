"use client";
import { useEffect, useState } from "react";

export default function ConfigPage() {
  const [yamlText, setYamlText] = useState("");
  const [parsed, setParsed] = useState<any>(null);
  const [path, setPath] = useState<string>("");
  const [parseError, setParseError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function load() {
    const r = await fetch("/api/config");
    const d = await r.json();
    setYamlText(d.yaml);
    setParsed(d.parsed);
    setPath(d.path);
    setParseError(d.parse_error ?? null);
  }

  useEffect(() => { load(); }, []);

  async function save() {
    setBusy(true);
    setSaved(null);
    try {
      const r = await fetch("/api/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ yaml: yamlText }),
      });
      if (r.status === 422) {
        const e = await r.json();
        setSaved(`解析错误: ${e.detail?.error ?? JSON.stringify(e.detail)}`);
        return;
      }
      const d = await r.json();
      setSaved(`已保存. 变更: ${d.changes?.length ? d.changes.join("; ") : "(none)"}`);
      await load();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">配置 (settings.yaml)</h1>
      <div className="text-xs text-fg-muted font-mono">{path}</div>
      {parseError && <div className="text-down text-sm">⚠ 解析错误: {parseError}</div>}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="bg-bg-card border border-bg-border rounded p-4">
          <div className="flex items-center justify-between mb-2">
            <div className="text-fg-muted text-xs">YAML</div>
            <div className="flex gap-2">
              <button onClick={load} className="text-xs text-fg-muted hover:text-fg">重载</button>
              <button onClick={save} disabled={busy} className="bg-accent text-bg font-semibold px-3 py-1 rounded text-sm disabled:opacity-50">保存</button>
            </div>
          </div>
          <textarea
            value={yamlText}
            onChange={(e) => setYamlText(e.target.value)}
            rows={24}
            className="w-full bg-bg border border-bg-border rounded p-2 font-mono text-xs num"
          />
          {saved && <div className="text-xs mt-2 text-fg-muted">{saved}</div>}
        </div>
        <div className="bg-bg-card border border-bg-border rounded p-4 max-h-[70vh] overflow-y-auto">
          <div className="text-fg-muted text-xs mb-2">解析预览</div>
          <pre className="text-xs whitespace-pre-wrap num text-fg">{parsed ? JSON.stringify(parsed, null, 2) : "(empty)"}</pre>
        </div>
      </div>
    </div>
  );
}

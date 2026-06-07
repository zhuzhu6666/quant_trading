"use client";
import { useEffect, useState } from "react";

interface BrokerStatus {
  mt5: { status: string; error?: string };
  ctrader: { status: string; error?: string };
}

export default function LivePage() {
  const [status, setStatus] = useState<BrokerStatus | null>(null);
  const [broker, setBroker] = useState<"mt5" | "ctrader">("mt5");
  const [symbol, setSymbol] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);

  async function load() {
    const r = await fetch("/api/live/status");
    setStatus(await r.json());
  }
  useEffect(() => { load(); }, []);

  async function emergencyClose() {
    if (!window.confirm(`确认紧急平仓 (${broker}${symbol ? " " + symbol : " 所有持仓"})?`)) return;
    setBusy(true);
    setResult(null);
    try {
      const r = await fetch("/api/live/emergency-close", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Confirm": "emergency" },
        body: JSON.stringify({ broker, symbol: symbol || null }),
      });
      const d = await r.json();
      setResult(d.ok ? `✓ ${d.broker} ${d.symbol} 已平` : `✗ ${d.error}`);
      await load();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">实盘</h1>
      <div className="bg-warn/10 border border-warn rounded p-3 text-sm text-warn">
        ⚠ MT5 阻塞: balance=0 (blocked-1) + Python MT5 包 vs terminal 2026 IPC pipe hash 不匹配 (blocked-2)。<br />
        cTrader token 已就位 (blocked-3 ✅)。此页面提供 UI + 紧急平仓;实盘 loop 仍需 `python main.py --mode live`。
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="bg-bg-card border border-bg-border rounded p-4">
          <div className="text-fg-muted text-sm mb-2">MT5</div>
          <div className="num text-xl">
            <span className={
              status?.mt5.status === "connected" ? "text-up" :
              status?.mt5.status === "disconnected" || status?.mt5.status === "no_token" ? "text-warn" :
              "text-down"
            }>{status?.mt5.status ?? "..."}</span>
          </div>
          {status?.mt5.error && <div className="text-xs text-fg-muted mt-1">{status.mt5.error}</div>}
        </div>
        <div className="bg-bg-card border border-bg-border rounded p-4">
          <div className="text-fg-muted text-sm mb-2">cTrader</div>
          <div className="num text-xl">
            <span className={
              status?.ctrader.status === "token_present" || status?.ctrader.status === "connected" ? "text-up" :
              status?.ctrader.status === "no_token" ? "text-warn" :
              "text-down"
            }>{status?.ctrader.status ?? "..."}</span>
          </div>
          {status?.ctrader.error && <div className="text-xs text-fg-muted mt-1">{status.ctrader.error}</div>}
        </div>
      </div>

      <div className="bg-bg-card border border-bg-border rounded p-4 space-y-3">
        <div className="text-fg-muted text-sm">紧急平仓</div>
        <div className="flex gap-3 items-end">
          <div>
            <label className="text-fg-muted text-xs">broker</label>
            <select value={broker} onChange={(e) => setBroker(e.target.value as "mt5" | "ctrader")} className="bg-bg border border-bg-border rounded px-2 py-1 text-sm">
              <option value="mt5">mt5</option>
              <option value="ctrader">ctrader</option>
            </select>
          </div>
          <div className="flex-1">
            <label className="text-fg-muted text-xs">symbol (留空 = 所有持仓)</label>
            <input type="text" value={symbol} onChange={(e) => setSymbol(e.target.value)} placeholder="XAUUSD+" className="w-full bg-bg border border-bg-border rounded px-2 py-1 text-sm" />
          </div>
          <button onClick={emergencyClose} disabled={busy} className="bg-down text-bg font-semibold px-4 py-2 rounded disabled:opacity-50">⏮ 紧急平仓</button>
        </div>
        {result && <div className={`text-sm ${result.startsWith("✓") ? "text-up" : "text-down"}`}>{result}</div>}
      </div>
    </div>
  );
}

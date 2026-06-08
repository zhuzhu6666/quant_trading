"use client";
import { useEffect, useState } from "react";
import { authFetch } from "@/lib/auth";
import { useAppStore } from "@/lib/store";
import { fmtNum, fmtPct, fmtUSD, classNames } from "@/lib/format";

interface BrokerStatus {
  mt5: { status: string; error?: string };
  ctrader: { status: string; error?: string };
  loop?: { running: boolean; pid?: number | null; broker?: string | null; started_at?: number | null };
}

interface AccountInfo {
  ok: boolean;
  broker: string;
  balance?: number;
  equity?: number;
  margin?: number;
  margin_free?: number;
  margin_level?: number;
  leverage?: number;
  currency?: string;
  error?: string;
}

interface Position {
  ticket: number;
  type: "buy" | "sell";
  volume: number;
  price_open: number;
  price_current: number;
  sl: number;
  tp: number;
  profit: number;
  magic?: number;
}

export default function LivePage() {
  const [status, setStatus] = useState<BrokerStatus | null>(null);
  const [account, setAccount] = useState<AccountInfo | null>(null);
  const [positions, setPositions] = useState<{ ok: boolean; positions: Position[]; error?: string } | null>(null);
  const [broker, setBroker] = useState<"mt5" | "ctrader">("mt5");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const snapshot = useAppStore((s) => s.snapshot);

  async function load() {
    try {
      const [sr, ar, pr] = await Promise.all([
        authFetch("/api/live/status"),
        authFetch(`/api/live/account?broker=${broker}`),
        authFetch(`/api/live/positions?broker=${broker}`),
      ]);
      if (sr.ok) setStatus(await sr.json());
      if (ar.ok) setAccount(await ar.json());
      if (pr.ok) setPositions(await pr.json());
    } catch (e) {
      // best-effort
    }
  }

  useEffect(() => { load(); }, [broker]);
  useEffect(() => {
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [broker]);

  async function emergencyClose() {
    if (!window.confirm(`确认紧急平仓 (${broker}${broker === "mt5" ? " 所有持仓" : ""})?\n后端 X-Confirm: emergency 二次校验。`)) return;
    setBusy(true);
    setResult(null);
    try {
      const r = await authFetch("/api/live/emergency-close", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Confirm": "emergency" },
        body: JSON.stringify({ broker, symbol: null }),
      });
      const d = await r.json();
      setResult(d.ok ? `✓ ${d.broker} ${d.symbol} 已平` : `✗ ${d.error || "failed"}`);
      await load();
    } finally {
      setBusy(false);
    }
  }

  const loopRunning = status?.loop?.running ?? false;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">实盘</h1>
        <div className="flex items-center gap-2 text-sm">
          <span className={classNames(
            "px-2 py-1 rounded text-xs font-semibold",
            snapshot?.source === "live" ? "bg-up/20 text-up" : "bg-fg-muted/20 text-fg-muted"
          )}>
            {snapshot?.source === "live" ? `● LIVE (${snapshot.broker})` : "● 离线"}
          </span>
        </div>
      </div>

      <div className="bg-warn/10 border border-warn rounded p-3 text-sm text-warn">
        MT5 / cTrader broker 配置 + 实时数据由 /api/live/* 提供。trading loop
        启停 / 账户信息 / 持仓在总览页 <a href="/" className="underline">/</a>
        集中控制。本页展示 broker 详情 (账户余额、保证金、当前持仓明细)。
      </div>

      {/* broker selector */}
      <div className="flex items-center gap-3">
        <label className="text-fg-muted text-sm">查看 broker:</label>
        <select value={broker} onChange={(e) => setBroker(e.target.value as "mt5" | "ctrader")} className="bg-bg border border-bg-border rounded px-2 py-1 text-sm">
          <option value="mt5">mt5</option>
          <option value="ctrader">ctrader</option>
        </select>
        <button onClick={load} className="text-xs text-fg-muted hover:text-fg">刷新</button>
        {loopRunning && status?.loop?.broker && status.loop.started_at != null && (
          <span className="text-xs text-up">loop: {status.loop.broker} (started {new Date(status.loop.started_at * 1000).toLocaleTimeString()})</span>
        )}
      </div>

      {/* broker connection status */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="bg-bg-card border border-bg-border rounded p-4">
          <div className="text-fg-muted text-sm mb-2">MT5 连接</div>
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
          <div className="text-fg-muted text-sm mb-2">cTrader 连接</div>
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

      {/* Account details */}
      <div className="bg-bg-card border border-bg-border rounded p-4">
        <div className="text-fg-muted text-sm mb-3">{broker.toUpperCase()} 账户详情</div>
        {account?.ok ? (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
            <Field label="余额">{fmtNum(account.balance ?? 0)} {account.currency}</Field>
            <Field label="净值">{fmtNum(account.equity ?? 0)} {account.currency}</Field>
            <Field label="已用保证金">{fmtNum(account.margin ?? 0)}</Field>
            <Field label="可用保证金">{fmtNum(account.margin_free ?? 0)}</Field>
            <Field label="保证金水平">{account.margin_level ? `${fmtNum(account.margin_level)}%` : "--"}</Field>
            <Field label="杠杆">1:{account.leverage ?? "--"}</Field>
            <Field label="币种">{account.currency ?? "--"}</Field>
          </div>
        ) : (
          <div className="text-fg-muted text-sm">
            {account?.error ?? "加载中..."}
          </div>
        )}
      </div>

      {/* Positions */}
      <div className="bg-bg-card border border-bg-border rounded p-4">
        <div className="text-fg-muted text-sm mb-3">{broker.toUpperCase()} 持仓 ({positions?.positions?.length ?? 0})</div>
        {positions?.ok && positions.positions.length > 0 ? (
          <table className="w-full text-sm num">
            <thead className="text-fg-muted">
              <tr className="border-b border-bg-border">
                <th className="text-right p-2">ticket</th>
                <th className="text-left p-2">type</th>
                <th className="text-right p-2">volume</th>
                <th className="text-right p-2">open</th>
                <th className="text-right p-2">current</th>
                <th className="text-right p-2">SL</th>
                <th className="text-right p-2">TP</th>
                <th className="text-right p-2">PnL</th>
              </tr>
            </thead>
            <tbody>
              {positions.positions.map((p) => (
                <tr key={p.ticket} className="border-b border-bg-border/50">
                  <td className="p-2 text-right text-fg-muted font-mono text-xs">{p.ticket}</td>
                  <td className={`p-2 ${p.type === "buy" ? "text-up" : "text-down"}`}>{p.type.toUpperCase()}</td>
                  <td className="p-2 text-right">{p.volume}</td>
                  <td className="p-2 text-right">{fmtNum(p.price_open)}</td>
                  <td className="p-2 text-right">{fmtNum(p.price_current)}</td>
                  <td className="p-2 text-right text-fg-muted">{p.sl ? fmtNum(p.sl) : "--"}</td>
                  <td className="p-2 text-right text-fg-muted">{p.tp ? fmtNum(p.tp) : "--"}</td>
                  <td className={`p-2 text-right ${p.profit >= 0 ? "text-up" : "text-down"}`}>{fmtUSD(p.profit)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="text-fg-muted text-sm">无持仓</div>
        )}
      </div>

      {/* Emergency close */}
      <div className="bg-bg-card border border-bg-border rounded p-4 space-y-3">
        <div className="text-fg-muted text-sm">紧急平仓 (所有持仓)</div>
        <div className="flex gap-3 items-end">
          <button onClick={emergencyClose} disabled={busy} className="bg-down text-bg font-semibold px-4 py-2 rounded disabled:opacity-50">⏮ 紧急平仓</button>
        </div>
        {result && <div className={`text-sm ${result.startsWith("✓") ? "text-up" : "text-down"}`}>{result}</div>}
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-fg-muted text-xs">{label}</div>
      <div className="text-fg num font-semibold">{children}</div>
    </div>
  );
}

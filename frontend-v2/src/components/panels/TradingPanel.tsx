import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Button, Card, Badge, Select, ConfirmDialog, Table } from "@/components/ui";
import type { Column } from "@/components/ui";
import { useConfirm, usePolling } from "@/lib/hooks";
import { authFetch } from "@/lib/auth";
import { useAppStore } from "@/lib/store";
import { fmtNum, fmtUSD } from "@/lib/format";
import { MiniAreaChart } from "@/components/dashboard/MiniAreaChart";

/* ─── Live types ─── */
interface BrokerStatus {
  ctrader: { status: string; error?: string };
  loop?: { running: boolean; pid?: number | null; broker?: string | null; started_at?: number | null };
}
interface AccountInfo { ok: boolean; broker: string; balance?: number; equity?: number; margin?: number; margin_free?: number; margin_level?: number; leverage?: number; currency?: string; error?: string; }
interface Position { ticket: number; type: "buy" | "sell"; volume: number; price_open: number; price_current: number; sl: number; tp: number; profit: number; magic?: number; }

function statusBadgeVariant(s: string | undefined): "success" | "warning" | "danger" {
  if (s === "connected") return "success";
  if (s === "no_token") return "warning";
  return "danger";
}

export default function TradingPanel() {
  return <LiveContent />;
}

/* ==================================================================
   Live — 实盘交易 (仅 cTrader)
   ================================================================== */
function LiveContent() {
  const [status, setStatus] = useState<BrokerStatus | null>(null);
  const [account, setAccount] = useState<AccountInfo | null>(null);
  const [positions, setPositions] = useState<{ ok: boolean; positions: Position[]; error?: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const { confirm, dialogProps } = useConfirm();
  const snapshot = useAppStore((s) => s.snapshot);

  async function load() {
    try {
      const [sr, ar, pr] = await Promise.all([
        authFetch("/api/live/status"),
        authFetch("/api/live/account?broker=ctrader"),
        authFetch("/api/live/positions?broker=ctrader"),
      ]);
      if (sr.ok) setStatus(await sr.json());
      if (ar.ok) setAccount(await ar.json());
      if (pr.ok) setPositions(await pr.json());
    } catch { /* best-effort */ }
  }
  useEffect(() => { load(); }, []);
  usePolling(load, 5000, []);

  async function emergencyClose() {
    const ok = await confirm("紧急平仓", "确认紧急平仓 (cTrader)? 后端 X-Confirm: emergency 二次校验。");
    if (!ok) return;
    setBusy(true); setResult(null);
    try {
      const r = await authFetch("/api/live/emergency-close", {
        method: "POST", headers: { "Content-Type": "application/json", "X-Confirm": "emergency" },
        body: JSON.stringify({ broker: "ctrader", symbol: null }),
      });
      const d = await r.json();
      setResult(d.ok ? `✓ ${d.broker} 已平` : `✗ ${d.error || "failed"}`);
      await load();
    } finally { setBusy(false); }
  }

  const loopRunning = status?.loop?.running ?? false;

  const columns: Column<Position>[] = [
    { key: "ticket", header: "ticket", align: "right", render: (p) => <span className="text-fg-muted font-mono text-xs">{p.ticket}</span> },
    { key: "type", header: "type", render: (p) => <span className={p.type === "buy" ? "text-up" : "text-down"}>{p.type.toUpperCase()}</span> },
    { key: "volume", header: "volume", align: "right", render: (p) => <>{p.volume}</> },
    { key: "open", header: "open", align: "right", render: (p) => <>{fmtNum(p.price_open)}</> },
    { key: "current", header: "current", align: "right", render: (p) => <>{fmtNum(p.price_current)}</> },
    { key: "sl", header: "SL", align: "right", render: (p) => <span className="text-fg-muted">{p.sl ? fmtNum(p.sl) : "--"}</span> },
    { key: "tp", header: "TP", align: "right", render: (p) => <span className="text-fg-muted">{p.tp ? fmtNum(p.tp) : "--"}</span> },
    { key: "pnl", header: "PnL", align: "right", render: (p) => <span className={p.profit >= 0 ? "text-up" : "text-down"}>{fmtUSD(p.profit)}</span> },
  ];

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">实盘</h1>
        <Badge variant={snapshot?.source === "live" ? "success" : "default"}>{snapshot?.source === "live" ? `● LIVE (${snapshot.broker})` : "● 离线"}</Badge>
      </div>
      <Card padding="sm" className="border-warn/30">
        <p className="text-sm text-warn">
          cTrader 实盘交易由 live_service 线程管理。启停 / 紧急平仓在总览页{" "}
          <Link to="/" className="underline underline-offset-2">集中控制</Link>。
          本页展示 broker 详情 (余额、保证金、持仓明细)。
        </p>
      </Card>
      <div className="flex items-end gap-4">
        <Button variant="ghost" size="sm" onClick={load}>刷新</Button>
        <Button variant="danger" size="sm" onClick={emergencyClose} disabled={busy}>⏮ 紧急平仓</Button>
        {loopRunning && status?.loop?.broker && status.loop.started_at != null && (
          <span className="text-xs text-up">loop: {status.loop.broker} (started {new Date(status.loop.started_at * 1000).toLocaleTimeString()})</span>
        )}
      </div>

      {/* Broker status */}
      <div className="grid grid-cols-1 gap-4">
        {(["ctrader"] as const).map((b) => (
          <Card key={b} title={b.toUpperCase()}>
            <div className="flex items-center gap-2 mb-2">
              <Badge variant={statusBadgeVariant((status as any)?.[b]?.status)}>{(status as any)?.[b]?.status ?? "?"}</Badge>
              {(status as any)?.[b]?.error && <span className="text-xs text-down">{(status as any)?.[b]?.error}</span>}
            </div>
            {account && (
              <div className="grid grid-cols-2 gap-2 text-xs">
                <div><span className="text-fg-muted">balance</span> <span className="num float-right">{fmtNum(account.balance ?? 0)}</span></div>
                <div><span className="text-fg-muted">equity</span> <span className="num float-right">{fmtNum(account.equity ?? 0)}</span></div>
                <div><span className="text-fg-muted">margin</span> <span className="num float-right">{fmtNum(account.margin ?? 0)}</span></div>
                <div><span className="text-fg-muted">margin_free</span> <span className="num float-right">{fmtNum(account.margin_free ?? 0)}</span></div>
                <div><span className="text-fg-muted">leverage</span> <span className="num float-right">{account.leverage ? `${account.leverage}:1` : "--"}</span></div>
                <div><span className="text-fg-muted">currency</span> <span className="num float-right">{account.currency ?? "--"}</span></div>
              </div>
            )}
          </Card>
        ))}
      </div>

      {/* ── 权益曲线 (从总览移入) ── */}
      <Card title="权益曲线" padding="md">
        <div className="h-20">
          <EquityChart />
        </div>
      </Card>

      {/* Positions table */}
      <Card title="当前持仓">
        <Table columns={columns} data={positions?.positions ?? []} keyExtractor={(p) => String(p.ticket)} emptyMessage={positions ? "无持仓" : "加载中..."} />
      </Card>
      {result && <div className={`text-sm ${result.startsWith("✓") ? "text-up" : "text-down"}`}>{result}</div>}
      <ConfirmDialog variant="danger" confirmLabel="紧急平仓" {...dialogProps} />
    </div>
  );
}

/* ── 权益曲线组件 (从 store 读取 equityHistory) ── */
function EquityChart() {
  const equityHistory = useAppStore((st) => st.equityHistory);
  const data = equityHistory.map((p) => p.v);
  return <MiniAreaChart data={data} height={80} color="#0071E3" className="w-full" showArea />;
}

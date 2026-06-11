import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Button, Card, Badge, MetricCard, Input, Select, ConfirmDialog, Table } from "@/components/ui";
import type { Column } from "@/components/ui";
import { useAliveRef, useConfirm, usePolling } from "@/lib/hooks";
import { authFetch } from "@/lib/auth";
import { useAppStore } from "@/lib/store";
import { EquityCurve, EquityPoint } from "@/components/charts/EquityCurve";
import { fmtNum, fmtUSD } from "@/lib/format";

/* ─── paper types ─── */
interface PaperStatus {
  status: "stopped" | "running" | "starting" | "stopping" | "error";
  pid?: number;
  started_at?: string;
  last_error?: string;
}

/* ─── Live types ─── */
interface BrokerStatus {
  mt5: { status: string; error?: string };
  ctrader: { status: string; error?: string };
  loop?: { running: boolean; pid?: number | null; broker?: string | null; started_at?: number | null };
}
interface AccountInfo { ok: boolean; broker: string; balance?: number; equity?: number; margin?: number; margin_free?: number; margin_level?: number; leverage?: number; currency?: string; error?: string; }
interface Position { ticket: number; type: "buy" | "sell"; volume: number; price_open: number; price_current: number; sl: number; tp: number; profit: number; magic?: number; }

const BROKER_OPTIONS = [{ value: "mt5", label: "mt5" }, { value: "ctrader", label: "ctrader" }];

function statusBadgeVariant(s: string | undefined): "success" | "warning" | "danger" {
  if (s === "connected") return "success";
  if (s === "no_token") return "warning";
  return "danger";
}

const tabs = ["paper", "live"] as const;
type Tab = (typeof tabs)[number];

export default function TradingPanel() {
  const [tab, setTab] = useState<Tab>("paper");
  const snapshot = useAppStore((s) => s.snapshot);

  return (
    <div className="space-y-4">
      <div className="flex gap-1 mb-4 p-1 rounded-lg" style={{ background: "rgba(255,255,255,0.5)" }}>
        {tabs.map((t) => (
          <button key={t}
            onClick={() => setTab(t)}
            className={`flex-1 py-1.5 px-3 rounded-md text-xs font-medium transition-all duration-200 ${tab === t ? "bg-[#d4edda] text-[#1a1e24]" : "bg-[#dce0e6] text-[#4a4f59] hover:bg-[#d0d5dd] hover:text-[#1a1e24]"}`}
          >{t === "paper" ? "模拟盘" : "实盘"}</button>
        ))}
      </div>
      {tab === "paper" && <PaperContent />}
      {tab === "live" && <LiveContent />}
    </div>
  );
}

/* ==================================================================
   Paper (模拟盘) — 改为状态展示, 不再手动启停
   ================================================================== */
function PaperContent() {
  const snapshot = useAppStore((s) => s.snapshot);
  const [status, setStatus] = useState<PaperStatus>({ status: "stopped" });
  const [equityPoints, setEquityPoints] = useState<EquityPoint[]>([]);
  const aliveRef = useAliveRef();
  const { confirm, dialogProps: confirmDialogProps } = useConfirm();

  // ── Scheduler 状态 (判断是否自主运行) ──
  const [schedRunning, setSchedRunning] = useState(false);
  useEffect(() => {
    authFetch("/api/control/scheduler").then(r => r.ok ? r.json().then(d => setSchedRunning(d.running)) : undefined).catch(() => {});
    const t = setInterval(() => {
      authFetch("/api/control/scheduler").then(r => r.ok ? r.json().then(d => setSchedRunning(d.running)) : undefined).catch(() => {});
    }, 10000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    if (!snapshot) return;
    setEquityPoints((prev) => {
      const last = prev[prev.length - 1];
      if (last?.v === snapshot.equity) return prev;
      const t = Math.floor(new Date(snapshot.server_time).getTime() / 1000);
      if (isNaN(t)) return prev;
      const next = [...prev, { t, v: snapshot.equity }];
      return next.length > 200 ? next.slice(-200) : next;
    });
  }, [snapshot]);

  async function refreshStatus() {
    const r = await authFetch("/api/paper/status");
    if (!aliveRef.current) return;
    if (r.ok) setStatus(await r.json());
  }

  useEffect(() => { refreshStatus(); }, []);

  const statusBadgeVariant: "success" | "default" | "warning" = status.status === "running" ? "success" : status.status === "stopped" ? "default" : "warning";
  const statusLabel = status.status === "running" ? `运行中 (pid ${status.pid})` : status.status === "stopped" ? "已停止 (由 InProcessScheduler 自动调度)" : status.status;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">模拟盘</h1>
        <Button variant="ghost" size="sm" onClick={refreshStatus}>刷新状态</Button>
      </div>

      {/* ── Status banner ── */}
      <Card>
        <div className="flex items-center gap-4">
          <div className="flex-1">
            <div className="text-sm text-fg-muted">状态</div>
            <div className="flex items-center gap-2 mt-1">
              <Badge variant={statusBadgeVariant}>{statusLabel}</Badge>
              {schedRunning && <Badge variant="success">● 自主调度</Badge>}
            </div>
            {status.started_at && <div className="text-xs text-fg-muted mt-1">启动于 {status.started_at}</div>}
            {status.last_error && <div className="text-xs text-down mt-1">{status.last_error}</div>}
            {schedRunning && (
              <div className="text-xs text-up mt-2">
                InProcessScheduler 已启动 — 模拟盘由 daily evolution cycle 自动管理。
                无需手动启停。
              </div>
            )}
          </div>
        </div>
      </Card>

      {/* ── Scheduler 任务状态 ── */}
      {schedRunning && (
        <Card>
          <div className="text-sm text-fg-muted mb-2">自动调度任务</div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-[11px]">
            <div className="flex items-center gap-1"><span className="w-1.5 h-1.5 rounded-full bg-up" /> 自进化循环 (每小时整点)</div>
            <div className="flex items-center gap-1"><span className="w-1.5 h-1.5 rounded-full bg-up" /> Canary 检查 (30min)</div>
            <div className="flex items-center gap-1"><span className="w-1.5 h-1.5 rounded-full bg-up" /> 因子退役 (每小时)</div>
            <div className="flex items-center gap-1"><span className="w-1.5 h-1.5 rounded-full bg-up" /> 数据同步 (5min)</div>
          </div>
        </Card>
      )}

      {/* ── Equity curve ── */}
      <Card>
        <div className="flex items-center justify-between mb-2">
          <div className="text-sm text-fg-muted">Equity 曲线</div>
          <div className="text-xs text-fg-muted">{equityPoints.length} 点 (最多 200)</div>
        </div>
        <EquityCurve points={equityPoints} height={240} />
      </Card>

      {/* ── Metrics ── */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <MetricCard label="Equity" value={snapshot ? fmtNum(snapshot.equity) : "--"} />
        <MetricCard label="今日 PnL" value={snapshot ? fmtUSD(snapshot.pnl_today) : "--"} trend={snapshot ? (snapshot.pnl_today >= 0 ? "up" : "down") : undefined} />
        <MetricCard label="今日交易" value={snapshot?.daily.trades ?? 0} subvalue={snapshot ? `胜 ${snapshot.daily.win ?? 0} / 负 ${snapshot.daily.loss ?? 0}` : undefined} />
        <MetricCard label="回撤" value={snapshot ? `${(snapshot.daily.drawdown_pct * 100).toFixed(1)}%` : "--"} subvalue={snapshot ? `连续亏损 ${snapshot.risk.consecutive_loss ?? 0}` : undefined} trend={snapshot && snapshot.daily.drawdown_pct > 0 ? "down" : undefined} />
      </div>
      <ConfirmDialog variant="danger" confirmLabel="紧急停止" {...confirmDialogProps} />
    </div>
  );
}

/* ==================================================================
   Live (实盘) tab — unchanged detail view
   ================================================================== */
function LiveContent() {
  const [status, setStatus] = useState<BrokerStatus | null>(null);
  const [account, setAccount] = useState<AccountInfo | null>(null);
  const [positions, setPositions] = useState<{ ok: boolean; positions: Position[]; error?: string } | null>(null);
  const [broker, setBroker] = useState<"mt5" | "ctrader">("ctrader");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const { confirm, dialogProps } = useConfirm();
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
    } catch { /* best-effort */ }
  }
  useEffect(() => { load(); }, [broker]);
  usePolling(load, 5000, [broker]);

  async function emergencyClose() {
    const ok = await confirm("紧急平仓", `确认紧急平仓 (${broker})? 后端 X-Confirm: emergency 二次校验。`);
    if (!ok) return;
    setBusy(true); setResult(null);
    try {
      const r = await authFetch("/api/live/emergency-close", {
        method: "POST", headers: { "Content-Type": "application/json", "X-Confirm": "emergency" },
        body: JSON.stringify({ broker, symbol: null }),
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
        <div className="w-28">
          <Select label="查看 broker" options={BROKER_OPTIONS} value={broker} onChange={(e) => setBroker(e.target.value as "mt5" | "ctrader")} />
        </div>
        <Button variant="ghost" size="sm" onClick={load}>刷新</Button>
        <Button variant="danger" size="sm" onClick={emergencyClose} disabled={busy}>⏮ 紧急平仓</Button>
        {loopRunning && status?.loop?.broker && status.loop.started_at != null && (
          <span className="text-xs text-up">loop: {status.loop.broker} (started {new Date(status.loop.started_at * 1000).toLocaleTimeString()})</span>
        )}
      </div>

      {/* Broker status */}
      <div className="grid grid-cols-2 gap-4">
        {(["mt5", "ctrader"] as const).map((b) => (
          <Card key={b} title={b.toUpperCase()}>
            <div className="flex items-center gap-2 mb-2">
              <Badge variant={statusBadgeVariant((status as any)?.[b]?.status)}>{(status as any)?.[b]?.status ?? "?"}</Badge>
              {(status as any)?.[b]?.error && <span className="text-xs text-down">{(status as any)?.[b]?.error}</span>}
            </div>
            {b === broker && account && (
              <div className="grid grid-cols-2 gap-2 text-xs">
                <div><span className="text-fg-muted">balance</span> <span className="num float-right">{fmtNum(account.balance ?? 0)}</span></div>
                <div><span className="text-fg-muted">equity</span> <span className="num float-right">{fmtNum(account.equity ?? 0)}</span></div>
                <div><span className="text-fg-muted">margin</span> <span className="num float-right">{fmtNum(account.margin ?? 0)}</span></div>
                <div><span className="text-fg-muted">margin_free</span> <span className="num float-right">{fmtNum(account.margin_free ?? 0)}</span></div>
                <div><span className="text-fg-muted">leverage</span> <span className="num float-right">{account.leverage ?? "--"}:1</span></div>
                <div><span className="text-fg-muted">currency</span> <span className="num float-right">{account.currency ?? "--"}</span></div>
              </div>
            )}
          </Card>
        ))}
      </div>

      {/* Positions table */}
      <Card title="当前持仓">
        <Table columns={columns} data={positions?.positions ?? []} keyExtractor={(p) => String(p.ticket)} emptyMessage={positions ? "无持仓" : "加载中..."} />
      </Card>
      {result && <div className={`text-sm ${result.startsWith("✓") ? "text-up" : "text-down"}`}>{result}</div>}
      <ConfirmDialog variant="danger" confirmLabel="紧急平仓" {...dialogProps} />
    </div>
  );
}

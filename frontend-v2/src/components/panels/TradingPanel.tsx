import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Button, Card, Badge, MetricCard, Input, Select, ConfirmDialog, Table } from "@/components/ui";
import type { Column } from "@/components/ui";
import { useAliveRef, useConfirm, usePolling } from "@/lib/hooks";
import { authFetch } from "@/lib/auth";
import { useAppStore } from "@/lib/store";
import { EquityCurve, EquityPoint } from "@/components/charts/EquityCurve";
import { classNames, fmtNum, fmtPct, fmtUSD } from "@/lib/format";

/* ───────── Paper types & consts ───────── */
interface PaperConfig {
  symbol: string;
  timeframe: string;
  use_router: boolean;
  use_scheduler: boolean;
  use_calibrator: boolean;
  use_factor_monitor: boolean;
  use_alerter: boolean;
  use_retrain: boolean;
  use_event_filter: boolean;
  include_shadow_factors: boolean;
  risk_per_trade_pct: number | null;
}

const DEFAULT_CONFIG: PaperConfig = {
  symbol: "XAUUSD+",
  timeframe: "M15",
  use_router: true,
  use_scheduler: true,
  use_calibrator: true,
  use_factor_monitor: true,
  use_alerter: true,
  use_retrain: true,
  use_event_filter: true,
  include_shadow_factors: false,
  risk_per_trade_pct: 1.0,
};

interface PaperStatus {
  status: "stopped" | "running" | "starting" | "stopping" | "error";
  pid?: number;
  started_at?: string;
  last_error?: string;
}

/* ───────── Live types & consts ───────── */
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

const BROKER_OPTIONS = [
  { value: "mt5", label: "mt5" },
  { value: "ctrader", label: "ctrader" },
];

function statusBadgeVariant(status: string | undefined): "success" | "warning" | "danger" {
  if (status === "connected") return "success";
  if (status === "no_token") return "warning";
  return "danger";
}

const tabs = ["paper", "live"] as const;
type Tab = (typeof tabs)[number];

export default function TradingPanel() {
  const [tab, setTab] = useState<Tab>("paper");
  const snapshot = useAppStore((s) => s.snapshot);

  return (
    <div className="space-y-4">
      {/* ── Tab bar ── */}
      <div className="flex gap-1 mb-4 p-1 rounded-lg" style={{ background: "rgba(255,255,255,0.5)" }}>
        {tabs.map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`flex-1 py-1.5 px-3 rounded-md text-xs font-medium transition-all duration-200 ${
              tab === t ? "bg-[#d4edda] text-[#1a1e24]" : "bg-[#dce0e6] text-[#4a4f59] hover:bg-[#d0d5dd] hover:text-[#1a1e24]"
            }`}
          >
            {t === "paper" ? "模拟盘" : "实盘"}
          </button>
        ))}
      </div>

      {tab === "paper" && <PaperContent />}
      {tab === "live" && <LiveContent />}
    </div>
  );
}

/* ==================================================================
   Paper (模拟盘) tab
   ================================================================== */
function PaperContent() {
  const snapshot = useAppStore((s) => s.snapshot);
  const [config, setConfig] = useState<PaperConfig>(DEFAULT_CONFIG);
  const [status, setStatus] = useState<PaperStatus>({ status: "stopped" });
  const [busy, setBusy] = useState(false);
  const [equityPoints, setEquityPoints] = useState<EquityPoint[]>([]);
  const [showConfig, setShowConfig] = useState(true);
  const aliveRef = useAliveRef();
  const { confirm, dialogProps: confirmDialogProps } = useConfirm();
  const [confirmVariant, setConfirmVariant] = useState<"danger" | "warning" | "primary">("danger");

  // Append equity point on every snapshot update
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

  async function start() {
    setBusy(true);
    try {
      const r = await authFetch("/api/paper/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(config),
      });
      if (!aliveRef.current) return;
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        alert(`启动失败: ${err.detail?.msg ?? r.statusText}`);
        return;
      }
      const d = await r.json();
      setStatus({ status: "running", pid: d.pid, started_at: d.started_at });
      setEquityPoints([]);
    } finally {
      if (aliveRef.current) setBusy(false);
    }
  }

  async function stop(close = false) {
    setBusy(true);
    try {
      const r = await authFetch("/api/paper/stop", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ close_positions: close }),
      });
      if (!aliveRef.current) return;
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        alert(`停止失败: ${err.detail?.msg ?? r.statusText}`);
        await refreshStatus();
        return;
      }
      const d = await r.json();
      setStatus({
        status: d.status ?? "stopped",
        last_error: d.error ?? undefined,
      });
      await refreshStatus();
      if (close && d.closed_positions != null) {
        alert(`已平 ${d.closed_positions} 个持仓`);
      }
    } catch (e: any) {
      if (aliveRef.current) alert(`停止失败: ${e?.message ?? "unknown"}`);
      await refreshStatus();
    } finally {
      if (aliveRef.current) setBusy(false);
    }
  }

  async function emergencyStop() {
    setConfirmVariant("danger");
    const ok = await confirm("紧急停止", "确认紧急停止? 后端会校验 X-Confirm: emergency header (二次校验)。");
    if (!ok) return;
    setBusy(true);
    try {
      const r = await authFetch("/api/paper/emergency-stop", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Confirm": "emergency" },
        body: JSON.stringify({ close_positions: true }),
      });
      if (!aliveRef.current) return;
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        alert(`紧急停止失败: ${err.detail?.msg ?? r.statusText}`);
        return;
      }
      await refreshStatus();
    } catch (e: any) {
      if (aliveRef.current) alert(`紧急停止失败: ${e?.message ?? "unknown"}`);
    } finally {
      if (aliveRef.current) setBusy(false);
    }
  }

  async function refreshStatus() {
    const r = await authFetch("/api/paper/status");
    if (!aliveRef.current) return;
    if (r.ok) setStatus(await r.json());
  }

  // Helpers for status display
  const statusBadgeVariant: "success" | "default" | "warning" = status.status === "running" ? "success" : status.status === "stopped" ? "default" : "warning";
  const statusLabel = status.status === "running" ? `运行中 (pid ${status.pid})` : status.status === "stopped" ? "已停止" : status.status;

  return (
    <div className="space-y-4">
      {/* ── Header ── */}
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">模拟盘</h1>
        <Button variant="ghost" size="sm" onClick={refreshStatus}>刷新状态</Button>
      </div>

      {/* ── Status card ── */}
      <Card>
        <div className="flex items-center gap-4">
          <div className="flex-1">
            <div className="text-sm text-fg-muted">状态</div>
            <div className="flex items-center gap-2 mt-1">
              <Badge variant={statusBadgeVariant}>{statusLabel}</Badge>
            </div>
            {status.started_at && <div className="text-xs text-fg-muted mt-1">启动于 {status.started_at}</div>}
            {status.last_error && <div className="text-xs text-down mt-1">{status.last_error}</div>}
          </div>
          <div className="flex gap-2">
            <Button variant="success" size="sm" onClick={start} disabled={busy || status.status === "running"}>
              ▶ 启动
            </Button>
            <Button variant="secondary" size="sm" onClick={() => stop(false)} disabled={busy || status.status === "stopped"}>
              ⏹ 停止
            </Button>
            <Button variant="danger" size="sm" onClick={emergencyStop} disabled={busy || status.status === "stopped"}>
              ⏮ 紧急停止
            </Button>
          </div>
        </div>
      </Card>

      {/* ── Config toggle ── */}
      <Button variant="ghost" size="sm" onClick={() => setShowConfig(!showConfig)}>
        {showConfig ? "▼" : "▶"} 配置
      </Button>

      {showConfig && (
      <Card className="mt-0">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
          <Input
            label="symbol"
            value="XAUUSD+"
            disabled
            className="text-fg-muted cursor-not-allowed"
          />
          <Select
            label="timeframe"
            value={config.timeframe}
            onChange={(e) => setConfig({ ...config, timeframe: e.target.value })}
            options={["M5","M15","M30","H1","H4","D1"].map(v => ({value: v, label: v}))}
          />
          <Input
            label="risk_per_trade_pct"
            type="number"
            step="0.1"
            value={config.risk_per_trade_pct ?? ""}
            onChange={(e) => setConfig({ ...config, risk_per_trade_pct: e.target.value === "" ? null : parseFloat(e.target.value) })}
            monospace
          />
          <div className="col-span-2 md:col-span-4 grid grid-cols-2 md:grid-cols-4 gap-2 pt-2">
            {([
              ["use_router", "MAB 路由"],
              ["use_scheduler", "调度器"],
              ["use_calibrator", "校准器"],
              ["use_factor_monitor", "因子监控"],
              ["use_alerter", "告警"],
              ["use_retrain", "重训"],
              ["use_event_filter", "事件过滤"],
              ["include_shadow_factors", "影子因子"],
            ] as const).map(([k, label]) => (
              <label key={k} className="flex items-center gap-2 cursor-pointer text-sm">
                <input
                  type="checkbox"
                  checked={config[k as keyof PaperConfig] as boolean}
                  onChange={(e) => setConfig({ ...config, [k as keyof PaperConfig]: e.target.checked })}
                  className="accent-[#58a6ff]"
                />
                <span>{label}</span>
              </label>
            ))}
          </div>
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

      {/* ── Metric cards ── */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <MetricCard
          label="Equity"
          value={snapshot ? fmtNum(snapshot.equity) : "--"}
        />
        <MetricCard
          label="今日 PnL"
          value={snapshot ? fmtUSD(snapshot.pnl_today) : "--"}
          trend={snapshot ? (snapshot.pnl_today >= 0 ? "up" : "down") : undefined}
        />
        <MetricCard
          label="今日交易"
          value={snapshot?.daily.trades ?? 0}
          subvalue={snapshot ? `胜 ${snapshot.daily.win ?? 0} / 负 ${snapshot.daily.loss ?? 0}` : undefined}
        />
        <MetricCard
          label="回撤"
          value={snapshot ? fmtPct(snapshot.daily.drawdown_pct) : "--"}
          subvalue={snapshot ? `连续亏损 ${snapshot.risk.consecutive_loss ?? 0}` : undefined}
          trend={snapshot && snapshot.daily.drawdown_pct > 0 ? "down" : undefined}
        />
      </div>

      {/* ConfirmDialog for emergency stop */}
      <ConfirmDialog variant={confirmVariant} confirmLabel="紧急停止" {...confirmDialogProps} />
    </div>
  );
}

/* ==================================================================
   Live (实盘) tab
   ================================================================== */
function LiveContent() {
  const [status, setStatus] = useState<BrokerStatus | null>(null);
  const [account, setAccount] = useState<AccountInfo | null>(null);
  const [positions, setPositions] = useState<{ ok: boolean; positions: Position[]; error?: string } | null>(null);
  const [broker, setBroker] = useState<"mt5" | "ctrader">("mt5");
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
    } catch {
      // best-effort
    }
  }

  // Immediate load on mount / broker change
  useEffect(() => { load(); }, [broker]);
  // Auto-refresh every 5s
  usePolling(load, 5000, [broker]);

  async function emergencyClose() {
    const ok = await confirm(
      "紧急平仓",
      `确认紧急平仓 (${broker}${broker === "mt5" ? " 所有持仓" : ""})?\n后端 X-Confirm: emergency 二次校验。`,
    );
    if (!ok) return;
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

  const columns: Column<Position>[] = [
    {
      key: "ticket",
      header: "ticket",
      align: "right",
      render: (p) => <span className="text-fg-muted font-mono text-xs">{p.ticket}</span>,
    },
    {
      key: "type",
      header: "type",
      align: "left",
      render: (p) => <span className={p.type === "buy" ? "text-up" : "text-down"}>{p.type.toUpperCase()}</span>,
    },
    { key: "volume", header: "volume", align: "right", render: (p) => <>{p.volume}</> },
    { key: "open", header: "open", align: "right", render: (p) => <>{fmtNum(p.price_open)}</> },
    { key: "current", header: "current", align: "right", render: (p) => <>{fmtNum(p.price_current)}</> },
    {
      key: "sl",
      header: "SL",
      align: "right",
      render: (p) => <span className="text-fg-muted">{p.sl ? fmtNum(p.sl) : "--"}</span>,
    },
    {
      key: "tp",
      header: "TP",
      align: "right",
      render: (p) => <span className="text-fg-muted">{p.tp ? fmtNum(p.tp) : "--"}</span>,
    },
    {
      key: "pnl",
      header: "PnL",
      align: "right",
      render: (p) => (
        <span className={p.profit >= 0 ? "text-up" : "text-down"}>{fmtUSD(p.profit)}</span>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">实盘</h1>
        <Badge variant={snapshot?.source === "live" ? "success" : "default"}>
          {snapshot?.source === "live" ? `● LIVE (${snapshot.broker})` : "● 离线"}
        </Badge>
      </div>

      {/* Info banner */}
      <Card padding="sm" className="border-warn/30">
        <p className="text-sm text-warn">
          MT5 / cTrader broker 配置 + 实时数据由 /api/live/* 提供。trading loop
          启停 / 账户信息 / 持仓在总览页{" "}
          <Link to="/" className="underline underline-offset-2">
            集中控制
          </Link>
          。本页展示 broker 详情 (账户余额、保证金、当前持仓明细)。
        </p>
      </Card>

      {/* Broker selector + loop info */}
      <div className="flex items-end gap-4">
        <div className="w-28">
          <Select
            label="查看 broker"
            options={BROKER_OPTIONS}
            value={broker}
            onChange={(e) => setBroker(e.target.value as "mt5" | "ctrader")}
          />
        </div>
        <Button variant="ghost" size="sm" onClick={load}>
          刷新
        </Button>
        {loopRunning && status?.loop?.broker && status.loop.started_at != null && (
          <span className="text-xs text-up">
            loop: {status.loop.broker} (started{" "}
            {new Date(status.loop.started_at * 1000).toLocaleTimeString()})
          </span>
        )}
      </div>

      {/* Broker connection status panels */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Card title="MT5 连接">
          <Badge variant={statusBadgeVariant(status?.mt5.status)}>
            {status?.mt5.status ?? "..."}
          </Badge>
          {status?.mt5.error && (
            <p className="text-xs text-fg-muted mt-2">{status.mt5.error}</p>
          )}
        </Card>
        <Card title="cTrader 连接">
          <Badge variant={statusBadgeVariant(status?.ctrader.status)}>
            {status?.ctrader.status ?? "..."}
          </Badge>
          {status?.ctrader.error && (
            <p className="text-xs text-fg-muted mt-2">{status.ctrader.error}</p>
          )}
        </Card>
      </div>

      {/* Account details */}
      <Card title={`${broker.toUpperCase()} 账户详情`}>
        {account?.ok ? (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <MetricCard
              label="余额"
              value={`${fmtNum(account.balance ?? 0)} ${account.currency ?? ""}`}
            />
            <MetricCard
              label="净值"
              value={`${fmtNum(account.equity ?? 0)} ${account.currency ?? ""}`}
            />
            <MetricCard label="已用保证金" value={fmtNum(account.margin ?? 0)} />
            <MetricCard label="可用保证金" value={fmtNum(account.margin_free ?? 0)} />
            <MetricCard
              label="保证金水平"
              value={account.margin_level ? `${fmtNum(account.margin_level)}%` : "--"}
            />
            <MetricCard label="杠杆" value={account.leverage ? `1:${account.leverage}` : "--"} />
            <MetricCard label="币种" value={account.currency ?? "--"} />
          </div>
        ) : (
          <p className="text-sm text-fg-muted">{account?.error ?? "加载中..."}</p>
        )}
      </Card>

      {/* Positions */}
      <Card
        title={`${broker.toUpperCase()} 持仓 (${positions?.positions?.length ?? 0})`}
      >
        <Table
          columns={columns}
          data={positions?.ok ? positions.positions : []}
          keyExtractor={(p) => String(p.ticket)}
          emptyMessage={positions === null ? "加载中..." : "无持仓"}
        />
      </Card>

      {/* Emergency close */}
      <Card title="紧急平仓" subtitle="所有持仓">
        <div className="flex items-center gap-3">
          <Button variant="danger" onClick={emergencyClose} loading={busy}>
            ⏮ 紧急平仓
          </Button>
          {result && (
            <span
              className={`text-sm ${result.startsWith("✓") ? "text-up" : "text-down"}`}
            >
              {result}
            </span>
          )}
        </div>
      </Card>

      <ConfirmDialog
        {...dialogProps}
        confirmLabel="确认平仓"
        variant="danger"
        loading={busy}
      />
    </div>
  );
}

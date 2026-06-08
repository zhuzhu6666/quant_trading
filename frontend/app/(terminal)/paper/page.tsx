"use client";
import { useEffect, useState } from "react";
import { authFetch } from "@/lib/auth";
import { useAppStore } from "@/lib/store";
import { EquityCurve, EquityPoint } from "@/components/charts/equity-curve";
import { fmtNum, fmtPct, fmtUSD, classNames } from "@/lib/format";

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

export default function PaperPage() {
  const snapshot = useAppStore((s) => s.snapshot);
  const [config, setConfig] = useState<PaperConfig>(DEFAULT_CONFIG);
  const [status, setStatus] = useState<PaperStatus>({ status: "stopped" });
  const [busy, setBusy] = useState(false);
  const [equityPoints, setEquityPoints] = useState<EquityPoint[]>([]);
  const [showConfig, setShowConfig] = useState(true);

  // Append equity point on every snapshot update (audit v5 fix B-4: was in render body,
  // now in useEffect to comply with React rules of no setState during render).
  useEffect(() => {
    if (!snapshot) return;
    setEquityPoints((prev) => {
      const last = prev[prev.length - 1];
      if (last?.v === snapshot.equity) return prev;  // dedup
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
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        alert(`启动失败: ${err.detail?.msg ?? r.statusText}`);
        return;
      }
      const d = await r.json();
      setStatus({ status: "running", pid: d.pid, started_at: d.started_at });
      setEquityPoints([]);  // reset curve on new run
    } finally {
      setBusy(false);
    }
  }

  async function stop(close = false) {
    setBusy(true);
    try {
      await authFetch("/api/paper/stop", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ close_positions: close }),
      });
      setStatus({ status: "stopped" });
    } finally {
      setBusy(false);
    }
  }

  async function emergencyStop() {
    // (audit v5 fix B-5: previous text mentioned a "5-second emergency input" that
    // never existed. The real second-factor is the X-Confirm: emergency header
    // sent below + a backend check in backend/api/paper.py:emergency_stop.)
    if (!window.confirm("确认紧急停止? 后端会校验 X-Confirm: emergency header (二次校验)。")) return;
    setBusy(true);
    try {
      await authFetch("/api/paper/emergency-stop", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Confirm": "emergency" },
        body: JSON.stringify({ close_positions: true }),
      });
      setStatus({ status: "stopped" });
    } finally {
      setBusy(false);
    }
  }

  async function refreshStatus() {
    const r = await authFetch("/api/paper/status");
    if (r.ok) setStatus(await r.json());
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">模拟盘</h1>
        <button onClick={refreshStatus} className="text-xs text-fg-muted hover:text-fg">刷新状态</button>
      </div>

      <div className="bg-bg-card border border-bg-border rounded p-4 flex items-center gap-4">
        <div className="flex-1">
          <div className="text-sm text-fg-muted">状态</div>
          <div className={classNames("text-xl font-semibold",
            status.status === "running" ? "text-up" :
            status.status === "stopped" ? "text-fg-muted" : "text-warn"
          )}>
            {status.status === "running" ? `运行中 (pid ${status.pid})` :
             status.status === "stopped" ? "已停止" :
             status.status}
          </div>
          {status.started_at && <div className="text-xs text-fg-muted mt-1">启动于 {status.started_at}</div>}
          {status.last_error && <div className="text-xs text-down mt-1">{status.last_error}</div>}
        </div>
        <div className="flex gap-2">
          <button onClick={start} disabled={busy || status.status === "running"} className="bg-up text-bg font-semibold px-4 py-2 rounded disabled:opacity-50">
            ▶ 启动
          </button>
          <button onClick={() => stop(false)} disabled={busy || status.status === "stopped"} className="bg-bg-border text-fg font-semibold px-4 py-2 rounded disabled:opacity-50">
            ⏹ 停止
          </button>
          <button onClick={emergencyStop} disabled={busy || status.status === "stopped"} className="bg-down text-bg font-semibold px-4 py-2 rounded disabled:opacity-50">
            ⏮ 紧急停止
          </button>
        </div>
      </div>

      <button onClick={() => setShowConfig(!showConfig)} className="text-sm text-fg-muted hover:text-fg">
        {showConfig ? "▼" : "▶"} 配置
      </button>
      {showConfig && (
        <div className="bg-bg-card border border-bg-border rounded p-4 grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
          <div>
            <label className="text-fg-muted text-xs">symbol</label>
            {/* (audit 2026-06-08: backend PaperStartRequest only supports
              XAUUSD+ (contract_size=100 oz/lot hard constraint in
              services/paper_service.py). A <select> with one option is
              misleading UX — replace with a disabled readonly text input.) */}
            <input
              type="text"
              value="XAUUSD+"
              disabled
              className="w-full bg-bg border border-bg-border rounded px-2 py-1 text-fg-muted cursor-not-allowed"
            />
          </div>
          <div>
            <label className="text-fg-muted text-xs">timeframe</label>
            <select value={config.timeframe} onChange={(e) => setConfig({ ...config, timeframe: e.target.value })} className="w-full bg-bg border border-bg-border rounded px-2 py-1">
              <option>M5</option><option>M15</option><option>M30</option>
              <option>H1</option><option>H4</option><option>D1</option>
            </select>
          </div>
          <div>
            <label className="text-fg-muted text-xs">risk_per_trade_pct</label>
            <input type="number" step="0.1" value={config.risk_per_trade_pct ?? ""} onChange={(e) => setConfig({ ...config, risk_per_trade_pct: e.target.value === "" ? null : parseFloat(e.target.value) })} className="w-full bg-bg border border-bg-border rounded px-2 py-1 num" />
          </div>
          <div className="col-span-2 md:col-span-4 grid grid-cols-2 md:grid-cols-4 gap-2 pt-2">
            {([
              ["use_router", "MAB 路由"], ["use_scheduler", "调度器"], ["use_calibrator", "校准器"],
              ["use_factor_monitor", "因子监控"], ["use_alerter", "告警"], ["use_retrain", "重训"],
              ["use_event_filter", "事件过滤"], ["include_shadow_factors", "影子因子"],
            ] as const).map(([k, label]) => (
              <label key={k} className="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" checked={config[k] as boolean} onChange={(e) => setConfig({ ...config, [k]: e.target.checked })} />
                <span>{label}</span>
              </label>
            ))}
          </div>
        </div>
      )}

      <div className="bg-bg-card border border-bg-border rounded p-4">
        <div className="flex items-center justify-between mb-2">
          <div className="text-sm text-fg-muted">Equity 曲线</div>
          <div className="text-xs text-fg-muted">{equityPoints.length} 点 (最多 200)</div>
        </div>
        <EquityCurve points={equityPoints} height={240} />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <div className="bg-bg-card border border-bg-border rounded p-4">
          <div className="text-xs text-fg-muted">Equity</div>
          <div className="num text-2xl">{snapshot ? fmtNum(snapshot.equity) : "--"}</div>
        </div>
        <div className="bg-bg-card border border-bg-border rounded p-4">
          <div className="text-xs text-fg-muted">今日 PnL</div>
          <div className={classNames("num text-2xl", (snapshot?.pnl_today ?? 0) >= 0 ? "text-up" : "text-down")}>
            {snapshot ? fmtUSD(snapshot.pnl_today) : "--"}
          </div>
        </div>
        <div className="bg-bg-card border border-bg-border rounded p-4">
          <div className="text-xs text-fg-muted">今日交易</div>
          <div className="num text-2xl">{snapshot?.daily.trades ?? 0}</div>
          <div className="text-xs text-fg-muted">胜 {snapshot?.daily.win ?? 0} / 负 {snapshot?.daily.loss ?? 0}</div>
        </div>
        <div className="bg-bg-card border border-bg-border rounded p-4">
          <div className="text-xs text-fg-muted">回撤</div>
          <div className="num text-2xl text-warn">{snapshot ? fmtPct(snapshot.daily.drawdown_pct) : "--"}</div>
          <div className="text-xs text-fg-muted">连续亏损 {snapshot?.risk.consecutive_loss ?? 0}</div>
        </div>
      </div>
    </div>
  );
}

import { useState, useEffect, useCallback } from "react";
import { useAppStore } from "@/lib/store";
import { fmtNum, fmtPct, fmtUSD } from "@/lib/format";
import { getWSClient } from "@/lib/ws";
import { authFetch } from "@/lib/auth";
import { KpiCard } from "@/components/dashboard/KpiCard";
import { DualRing } from "@/components/dashboard/DualRing";
import { FunctionButton } from "@/components/dashboard/FunctionButton";
import { SlidePanel } from "@/components/dashboard/SlidePanel";
import { Card } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import TradingPanel from "@/components/panels/TradingPanel";
import FactorsPanel from "@/components/panels/FactorsPanel";
import ExperimentsPanel from "@/components/panels/ExperimentsPanel";
import DataPanel from "@/components/panels/DataPanel";
import SystemPanel from "@/components/panels/SystemPanel";
import RiskPanel from "@/components/panels/RiskPanel";
import BacktestPanel from "@/components/panels/BacktestPanel";
import LogCard from "@/components/dashboard/LogCard";

type PanelType = "trading" | "factors" | "experiments" | "data" | "system" | "risk" | "ops" | "backtest" | null;

/* ─── Scheduler job type ─── */
interface SchedJob {
  name: string;
  cron_expr: string;
  running: boolean;
  run_count: number;
  error_count: number;
  last_error: string;
  next_run_time: number;
}

interface SchedStatus {
  running: boolean;
  jobs: SchedJob[];
  error?: string;
}

const JOB_LABELS: Record<string, string> = {
  evolution_hourly: "自进化循环 (GP→OOS→Canary→退役→权重)",
  sync_health: "数据同步健康",
  data_pull: "数据拉取 (cTrader→DataStore)",
  data_sync: "数据同步 (增量补 bar)",
  dukascopy_tick: "Dukascopy tick 增量",
  system_health: "系统健康检查",
  awe_adapt: "AWE 权重自适应",
  ml_retrain: "ML 方向预测器 (XGBoost 重训)",
  feature_eng: "特征工程 (PCA 主成分分析)",
  ml_drift_check: "概念漂移检测 (ML 模型健康)",
};

const CRON_LABELS: Record<string, string> = {
  "0 * * * *": "每小时整点",
  "*/30 * * * *": "每 30 分钟",
  "*/5 * * * *": "每 5 分钟",
  "* * * * *": "每分钟",
  "0 5 * * 0": "每周日 05:00",
  "0 3 * * *": "每日 03:00",
  "0 */6 * * *": "每 6 小时",
};

export default function MainDashboard() {
  const { snapshot, wsConnected } = useAppStore();
  const [activePanel, setActivePanel] = useState<PanelType>(null);
  const [err, setErr] = useState<string | null>(null);

  // ── Scheduler status (5s polling) ──
  const [sched, setSched] = useState<SchedStatus | null>(null);
  const refreshSched = useCallback(async () => {
    try {
      const r = await authFetch("/api/control/scheduler");
      if (r.ok) setSched(await r.json());
    } catch { /* best-effort */ }
  }, []);
  useEffect(() => { refreshSched(); const t = setInterval(refreshSched, 5000); return () => clearInterval(t); }, [refreshSched]);

  // ── Factor V4 weight history + attribution stats ──
  const [factorWeights, setFactorWeights] = useState<{ factor: string; weight: number }[]>([]);

  const refreshFactorWeights = useCallback(async () => {
    try {
      const r = await authFetch("/api/v4/weights");
      if (r.ok) {
        const data = await r.json();
        if (Array.isArray(data) && data.length > 0) {
          const latest = new Map<string, number>();
          for (const entry of data) {
            if (entry.factor && entry.new !== undefined) {
              latest.set(entry.factor, entry.new);
            }
          }
          const sorted = [...latest.entries()]
            .map(([factor, weight]) => ({ factor, weight }))
            .sort((a, b) => b.weight - a.weight);
          setFactorWeights(sorted);
        }
      }
    } catch { /* best-effort */ }
  }, []);

  useEffect(() => {
    refreshFactorWeights();
    const t = setInterval(refreshFactorWeights, 10000);
    return () => clearInterval(t);
  }, [refreshFactorWeights]);

  // ── Live loop status ──
  const [loopBusy, setLoopBusy] = useState(false);
  const [loopStatus, setLoopStatus] = useState<{ running: boolean; broker: string | null; started_at: number | null; pid: number | null }>(
    { running: false, broker: null, started_at: null, pid: null }
  );
  const [loopMsg, setLoopMsg] = useState<string | null>(null);
  // ★ 实时的 cTrader 连接状态 (HTTP 轮询, 不依赖 WS)
  const [ctraderConnected, setCtraderConnected] = useState(false);

  const refreshLoop = useCallback(async () => {
    try {
      const r = await authFetch("/api/live/loop-status");
      if (r.ok) setLoopStatus(await r.json());
    } catch { /* best-effort */ }
  }, []);

  // ★ 每 5s 直接查询 cTrader 连接状态 (HTTP, 解决 WS 不稳定问题)
  const refreshCtraderStatus = useCallback(async () => {
    try {
      const r = await authFetch("/api/live/status");
      if (r.ok) {
        const data = await r.json();
        setCtraderConnected(data?.ctrader?.status === "connected");
      } else {
        setCtraderConnected(false);
      }
    } catch { setCtraderConnected(false); }
  }, []);
  // ★ 每 5s HTTP 轮询 cTrader 连接状态 (不依赖不稳定的 WS)
  useEffect(() => { refreshCtraderStatus(); const t = setInterval(refreshCtraderStatus, 5000); return () => clearInterval(t); }, [refreshCtraderStatus]);

  // ★ WS 后备1: HTTP 轮询账户数据, WS 断连时仍有实时数字
  const [httpEquity, setHttpEquity] = useState<number | null>(null);
  const [httpBalance, setHttpBalance] = useState<number | null>(null);
  const refreshAccount = useCallback(async () => {
    try {
      const r = await authFetch('/api/live/account?broker=ctrader');
      if (r.ok) {
        const data = await r.json();
        if (data?.ok) {
          if (data.equity != null) setHttpEquity(data.equity);
          if (data.balance != null) setHttpBalance(data.balance);
        }
      }
    } catch { /* best-effort */ }
  }, []);
  useEffect(() => { refreshAccount(); const t = setInterval(refreshAccount, 10000); return () => clearInterval(t); }, [refreshAccount]);

  // ★ WS 后备2: HTTP 轮询策略状态 (持仓方向/风控), WS 断连时仍有数据
  const [httpPnl, setHttpPnl] = useState(0);
  const [httpDir, setHttpDir] = useState<string | null>(null);
  const [httpTrades, setHttpTrades] = useState(0);
  const [httpCircuit, setHttpCircuit] = useState(false);
  const refreshStrategy = useCallback(async () => {
    try {
      const r = await authFetch("/api/live/strategy-status");
      if (r.ok) {
        const d = await r.json();
        if (d.position) setHttpDir(d.position.dir === "LONG" ? "LONG" : d.position.dir === "SHORT" ? "SHORT" : null);
        setHttpCircuit(!!d.circuit_breaker);
      }
    } catch { /* best-effort */ }
  }, []);
  useEffect(() => { refreshStrategy(); const t = setInterval(refreshStrategy, 10000); return () => clearInterval(t); }, [refreshStrategy]);

  // ★ WS 后备3: HTTP 轮询会话统计 (今日盈亏/交易数)
  const refreshSession = useCallback(async () => {
    try {
      const r = await authFetch("/api/live/session-stats");
      if (r.ok) {
        const d = await r.json();
        if (d.pnl_today != null) setHttpPnl(d.pnl_today);
        if (d.trades != null) setHttpTrades(d.trades);
      }
    } catch { /* best-effort */ }
  }, []);
  useEffect(() => { refreshSession(); const t = setInterval(refreshSession, 10000); return () => clearInterval(t); }, [refreshSession]);

  useEffect(() => { refreshLoop(); const t = setInterval(refreshLoop, 5000); return () => clearInterval(t); }, [refreshLoop]);

  // Auto-clear loop message after 8s
  useEffect(() => {
    if (!loopMsg) return;
    const t = setTimeout(() => setLoopMsg(null), 8000);
    return () => clearTimeout(t);
  }, [loopMsg]);

  async function startLoop() {
    setLoopBusy(true); setLoopMsg(null);
    try {
      const ar = await authFetch('/api/live/account?broker=ctrader');
      if (!ar.ok) { setLoopMsg('✗ ctrader 账户预飞失败: HTTP ' + ar.status); return; }
      const ad = await ar.json();
      if (!ad.ok) { setLoopMsg('✗ ctrader 不可达: ' + (ad.error || 'unknown')); return; }
      const r = await authFetch("/api/live/start", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ broker: "ctrader", strategy_name: "factor_v4" }) });
      if (!r.ok) { setLoopMsg(`✗ 启动失败: HTTP ${r.status}`); return; }
      const d = await r.json();
      setLoopMsg(`✓ ${d.broker} loop started (pid ${d.pid})`);
      refreshLoop();
    } catch (e: any) { setLoopMsg(`✗ ${e?.message ?? e}`); }
    finally { setLoopBusy(false); }
  }

  async function stopLoop() {
    setLoopBusy(true); setLoopMsg(null);
    try {
      const r = await authFetch("/api/live/stop", { method: "POST" });
      if (!r.ok) { setLoopMsg(`✗ 停止失败: HTTP ${r.status}`); return; }
      const d = await r.json();
      setLoopMsg(d.was_running ? "✓ loop stopped" : "loop 原本未运行");
      refreshLoop();
    } catch (e: any) { setLoopMsg(`✗ ${e?.message ?? e}`); }
    finally { setLoopBusy(false); }
  }

  async function emergencyClose() {
    if (!window.confirm('确认紧急平仓 ctrader 所有持仓? (后端 X-Confirm)')) return;
    setLoopBusy(true); setLoopMsg(null);
    try {
      const r = await authFetch("/api/live/emergency-close", { method: "POST", headers: { "Content-Type": "application/json", "X-Confirm": "emergency" }, body: JSON.stringify({ broker: "ctrader", symbol: null }) });
      if (!r.ok) {
        const errBody = await r.json().catch(() => ({}));
        setLoopMsg(`✗ HTTP ${r.status}: ${errBody?.detail?.msg || errBody?.detail || r.statusText}`);
        return;
      }
      const d = await r.json();
      setLoopMsg(d.ok ? `✓ ${d.broker} 已平` : `✗ ${d.error || "failed"}`);
    } catch (e: any) { setLoopMsg(`✗ ${e?.message ?? e}`); }
    finally { setLoopBusy(false); }
  }

  const s = snapshot;
  const equityHistory = useAppStore((st) => st.equityHistory);
  const equityData = equityHistory.map((p) => p.v);
  // 优先 WS 实时数据, 后备 HTTP 轮询数据 (WS 在 Windows 不稳定)
  const effectiveEquity = (s?.equity != null) ? s.equity : (httpEquity ?? 1000);
  const effectiveBalance = (s?.balance != null) ? s.balance : (httpBalance ?? 1000);
  const equity = effectiveEquity;
  const balance = effectiveBalance;
  const pnl = (s?.pnl_today != null) ? s.pnl_today : httpPnl;
  const dir = s?.position?.dir ?? httpDir ?? "FLAT";
  const trades = (s?.daily?.trades != null) ? s.daily.trades : httpTrades;
  const wins = s?.daily?.win ?? 0;
  const losses = s?.daily?.loss ?? 0;
  const winRate = trades > 0 ? Math.round((wins / trades) * 100) : 0;
  const dd = s?.daily?.drawdown_pct ?? 0;
  const circuit = s?.risk?.circuit_breaker ?? httpCircuit;
  const consecLoss = s?.risk?.consecutive_loss ?? 0;

  const source: "live" | "paper" | "none" = (s?.source as any) ?? (loopStatus.running ? "live" : "none");

  const panels: { key: PanelType; icon: string; label: string; desc: string; accent: string }[] = [
    { key: "trading", icon: "💹", label: "交易", desc: "模拟盘 · 实盘 · 风控", accent: "#0071E3" },
    { key: "factors", icon: "🔬", label: "因子", desc: "健康 · 发现 · 影子", accent: "#34C759" },
    { key: "experiments", icon: "🧪", label: "实验", desc: "调参 · 校准 · A/B", accent: "#FF9500" },
    { key: "data", icon: "📈", label: "数据", desc: "K线 · 外部数据 · 同步", accent: "#AF52DE" },
    { key: "system", icon: "⚙️", label: "系统", desc: "报告 · 配置 · 任务 · 恢复 · 周报", accent: "#5856D6" },
    { key: "risk", icon: "🛡️", label: "风控", desc: "VaR · Kelly · 压力测试", accent: "#FF3B30" },
    { key: "backtest", icon: "📊", label: "回测", desc: "向量回测 · 历史 · 对比", accent: "#5AC8FA" },
  ];

  if (err) {
    return (
      <div className="min-h-screen flex items-center justify-center text-sm text-danger">
        {err}
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-apple-bg text-text-primary">
      {/* ── Navigation Bar ── */}
      <nav className="nav-bar">
        <div className="max-w-[1600px] mx-auto h-full flex items-center justify-between px-4 md:px-6">
          <div className="flex items-center gap-3">
            <span className="text-xl font-semibold tracking-tight text-accent">
              ◆ Quant
            </span>
            <Badge
              variant={source === "live" ? "danger" : source === "paper" ? "warning" : "default"}
              dot
            >
              {source === "live" ? `LIVE (${loopStatus.broker ?? "ctrader"})` : source === "paper" ? "PAPER" : "离线"}
            </Badge>
            {sched?.running && loopStatus.running && (
              <Badge variant="success" dot>自主运行</Badge>
            )}
          </div>
          <div className="flex items-center gap-4 text-sm">
            <Badge variant="accent" className="font-medium">XAUUSD+</Badge>
            <span className="num font-semibold text-text-primary">{fmtNum(equity)}</span>
            <span className={`num font-semibold ${pnl >= 0 ? "text-success" : "text-danger"}`}>
              {fmtPct(pnl)}
            </span>
            {dir !== "FLAT" && (
              <span className={`font-semibold ${dir === "LONG" ? "text-success" : "text-danger"}`}>
                {dir}
              </span>
            )}
            <div className="flex items-center gap-1.5">
              <span className={`w-2 h-2 rounded-full ${ctraderConnected ? "bg-success" : "bg-warning"} ${ctraderConnected ? "animate-pulse-soft" : ""}`} />
              <span className={`text-xs ${ctraderConnected ? "text-success" : "text-warning"}`}>
                {ctraderConnected ? "已连接" : "断开"}
              </span>
            </div>
          </div>
        </div>
      </nav>

      {/* ── Main Content ── */}
      <main className="pt-[68px] pb-6 px-4 md:px-6">
        <div className="max-w-[1600px] mx-auto space-y-5">

          {/* ── KPI Row ── */}
          <div className="grid grid-cols-2 md:grid-cols-6 gap-4">
            <div className="md:col-span-1 col-span-2">
              <KpiCard
                label="账户权益"
                value={fmtNum(equity)}
                subvalue={`余额 ${fmtNum(balance)}`}
                trend="neutral"
                chart={equityData}
              />
            </div>
            <KpiCard
              label="今日盈亏"
              value={fmtUSD(pnl)}
              subvalue={`交易 ${trades} | 胜 ${wins} | 负 ${losses}`}
              trend={pnl >= 0 ? "up" : "down"}
            />
            <div className="flex items-center justify-center">
              <DualRing
                outerValue={winRate}
                outerLabel="胜率"
                innerValue={Math.round(dd * 100)}
                innerLabel="回撤"
                size={72}
                className="scale-95 md:scale-100"
              />
            </div>
            <KpiCard
              label="持仓"
              value={dir}
              subvalue={s?.position?.dir !== "FLAT" && s ? `@ ${fmtNum(s.position.entry)}` : undefined}
              trend={dir === "LONG" ? "up" : dir === "SHORT" ? "down" : "neutral"}
            />
            <KpiCard
              label="风控"
              value={circuit ? "熔断" : "正常"}
              subvalue={`连亏 ${consecLoss} 次`}
              trend={circuit ? "down" : "neutral"}
            />
          </div>

          {/* ── 三栏: 自主运行 | 因子管道 | 近期开仓 ── */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            {/* ── 自主运行 ── */}
            <Card className="flex flex-col" padding="md">
              {/* Status Header */}
              <div className="flex items-center justify-between mb-4">
                <div className="flex items-center gap-2.5">
                  <span className={`w-2.5 h-2.5 rounded-full flex-shrink-0 ${
                    loopStatus.running
                      ? (sched?.running ? "bg-success" : "bg-warning")
                      : "bg-text-tertiary"
                  }`} />
                  <span className="text-sm font-semibold text-text-primary">
                    {loopStatus.running ? (sched?.running ? "自主运行中" : "启动中...") : "等待启动"}
                  </span>
                </div>
                <div className="flex items-center gap-3 text-xs text-text-secondary">
                  {loopStatus.running && (
                    <span className="font-semibold text-success">{loopStatus.broker}</span>
                  )}
                  <div className="flex items-center gap-1">
                    <span className={`w-1.5 h-1.5 rounded-full ${ctraderConnected ? "bg-success" : "bg-warning"}`} />
                    <span className={ctraderConnected ? "text-success" : "text-warning"}>
                      {ctraderConnected ? "已连接" : "断开"}
                    </span>
                  </div>
                </div>
              </div>

              {/* Scheduler Jobs */}
              <div className="flex-1 overflow-y-auto min-h-0 space-y-1">
                {(sched?.jobs ?? []).map((j) => (
                  <div key={j.name} className="grid grid-cols-[1fr_auto] gap-2 items-center py-1.5 border-b border-apple-divider last:border-0">
                    <div className="flex items-center gap-2.5 min-w-0">
                      <span className={`w-2 h-2 rounded-full flex-shrink-0 ${
                        j.error_count > 0 ? "bg-danger" : j.run_count > 0 ? "bg-success" : "bg-text-tertiary"
                      }`} />
                      <span className="text-xs text-text-primary truncate" title={JOB_LABELS[j.name] ?? j.name}>
                        {JOB_LABELS[j.name] ?? j.name}
                      </span>
                    </div>
                    <div className="flex items-center gap-2 flex-shrink-0 text-2xs">
                      <span className="text-text-secondary whitespace-nowrap" title={j.cron_expr}>
                        {CRON_LABELS[j.cron_expr] ?? j.cron_expr}
                      </span>
                      <span className="text-text-secondary whitespace-nowrap">
                        {j.run_count} 次
                      </span>
                      {j.error_count > 0 && (
                        <span className="text-danger font-semibold whitespace-nowrap">{j.error_count} 次错误</span>
                      )}
                    </div>
                  </div>
                ))}
                {(!sched?.jobs || sched.jobs.length === 0) && (
                    <div className="text-xs text-text-secondary py-4 text-center">
                      {loopStatus.running ? "调度任务加载中..." : "启动实盘后自动注册调度任务"}
                    </div>
                  )}

                  {/* 实时数据流 (非定时任务) */}
                  <div className="grid grid-cols-[1fr_auto] gap-2 items-center py-1.5 border-b border-apple-divider last:border-0">
                    <div className="flex items-center gap-2.5 min-w-0">
                      <span className="w-2 h-2 rounded-full flex-shrink-0 bg-success" />
                      <span className="text-xs text-text-primary truncate">L2 订单簿 (深度报价)</span>
                    </div>
                    <div className="flex items-center gap-2 flex-shrink-0 text-2xs">
                      <span className="text-accent-cyan whitespace-nowrap">● 实时推送</span>
                    </div>
                  </div>
                </div>

              {/* System Status */}
              <div className="mt-4 pt-3 border-t border-apple-divider flex items-center gap-5 text-xs">
                <div className="flex items-center gap-1.5">
                  <span className="text-text-secondary">熔断</span>
                  <span className={`font-semibold ${circuit ? "text-danger" : "text-success"}`}>
                    {circuit ? "触发" : "正常"}
                  </span>
                </div>
                <div className="flex items-center gap-1.5">
                  <span className="text-text-secondary">连亏</span>
                  <span className="font-semibold text-text-primary">{consecLoss} 次</span>
                </div>
                <div className="flex items-center gap-1.5">
                  <span className="text-text-secondary">杠杆</span>
                  <span className="font-semibold text-text-primary">
                    {s?.leverage ? `${s.leverage}:1` : "--"}
                  </span>
                </div>
              </div>

              {/* Control Buttons */}
              <div className="mt-4 pt-3 border-t border-apple-divider">
                <div className="flex items-center gap-2.5">
                  <button
                    className={`flex-1 py-2 rounded-xl text-sm font-semibold text-white transition-all duration-200 hover:-translate-y-0.5 active:scale-[0.97] disabled:opacity-40 ${
                      loopStatus.running
                        ? "bg-warning hover:shadow-lg"
                        : "bg-success hover:shadow-lg"
                    }`}
                    onClick={loopStatus.running ? stopLoop : startLoop}
                    disabled={loopBusy}
                    data-testid="main-toggle-loop"
                  >
                    {loopBusy ? "..." : loopStatus.running ? "⏹ 停止实盘" : "▶ 启动 cTrader 实盘"}
                  </button>
                  <button
                    className="py-2 px-4 rounded-xl text-sm font-semibold text-white transition-all duration-200 hover:-translate-y-0.5 active:scale-[0.97] bg-danger hover:shadow-lg"
                    onClick={emergencyClose}
                    disabled={loopBusy}
                    data-testid="main-emergency-close"
                  >
                    ⏮ 紧急平仓
                  </button>
                </div>
                {loopMsg && (
                  <div className={`text-xs mt-2 ${loopMsg.startsWith("✓") ? "text-success" : "text-danger"}`} data-testid="main-loop-msg">
                    {loopMsg}
                  </div>
                )}
                <div className="text-2xs text-text-secondary mt-1.5">
                  {loopStatus.running
                    ? sched?.running ? "自进化已启动，点击停止后调度自动终止" : "调度任务加载中..."
                    : "启动后自动拉数据 → 健康检查 → Canary → 退役，按定时频率自动运行"}
                </div>
              </div>
            </Card>

            {/* ── 因子管道 (AWE 权重排名 + 因子状态) ── */}
            <Card title="因子管道" padding="sm">
              {factorWeights.length > 0 ? (
                <div className="space-y-1">
                  <div className="grid grid-cols-[1fr_4rem_3.5rem] gap-1 text-2xs text-text-secondary mb-2 px-1">
                    <span>因子名</span>
                    <span className="text-right">权重</span>
                    <span className="text-center">状态</span>
                  </div>
                  <div className="text-2xs text-text-tertiary px-1 pb-1 border-b border-apple-divider">
                    AWE 按因子历史表现自动调权重 (≥1.0=强化, 0.1~1.0=正常, &lt;0.1=弱化)
                  </div>
                  {factorWeights.slice(0, 15).map((fw) => (
                    <div key={fw.factor} className="grid grid-cols-[1fr_4rem_3.5rem] gap-1 items-center py-1 px-1 text-xs border-b border-apple-divider last:border-0 rounded hover:bg-apple-bg/40">
                      <span className="text-text-primary truncate" title={fw.factor}>{fw.factor}</span>
                      <span className={`font-semibold num text-right ${fw.weight >= 1.0 ? "text-success" : fw.weight > 0.1 ? "text-text-primary" : "text-text-tertiary"}`}>
                        {fw.weight.toFixed(3)}
                      </span>
                      <span className={`text-2xs px-1 py-0.5 rounded font-medium text-center ${
                        fw.weight >= 1.0
                          ? "bg-success/10 text-success"
                          : fw.weight > 0.1
                            ? "bg-neutral/10 text-text-primary"
                            : fw.weight > 0.05
                              ? "bg-warning/10 text-warning"
                              : "bg-text-tertiary/10 text-text-tertiary"
                      }`}>
                        {fw.weight >= 1.0 ? "强化" : fw.weight > 0.1 ? "正常" : fw.weight > 0.05 ? "弱" : "休眠"}
                      </span>
                    </div>
                  ))}
                  {factorWeights.length > 15 && (
                    <div className="text-2xs text-text-tertiary text-center pt-1">
                      还有 {factorWeights.length - 15} 个因子 · AWE 每 30 分钟自适应更新
                    </div>
                  )}
                  {factorWeights.length <= 15 && (
                    <div className="text-2xs text-text-tertiary text-center pt-1">
                      AWE 每 30 分钟自适应更新权重
                    </div>
                  )}
                </div>
              ) : (
                <div className="text-sm text-text-secondary py-4 text-center">等待实盘启动后因子数据</div>
              )}
            </Card>

            {/* ── 近期开仓 (因子通过闸门历史) ── */}
            <RecentTicksCard />
          </div>

          {/* ── 实时日志 (全宽拉伸, 详情面板上方) ── */}
          <div className="w-full">
            <LogCard />
          </div>

          {/* ── Function Buttons ── */}
          <div>
            <div className="section-label mb-3">详情面板</div>
            <div className="grid grid-cols-4 md:grid-cols-8 gap-3">
              {panels.map((p) => (
                <FunctionButton
                  key={p.key}
                  icon={p.icon}
                  label={p.label}
                  description={p.desc}
                  accent={p.accent}
                  onClick={() => setActivePanel(p.key)}
                />
              ))}
            </div>
          </div>
        </div>
      </main>

      {/* Slide Panels */}
      <SlidePanel open={activePanel === "trading"} onClose={() => setActivePanel(null)} title="交易管理">
        <TradingPanel />
      </SlidePanel>
      <SlidePanel open={activePanel === "factors"} onClose={() => setActivePanel(null)} title="因子管理">
        <FactorsPanel />
      </SlidePanel>
      <SlidePanel open={activePanel === "experiments"} onClose={() => setActivePanel(null)} title="实验工具">
        <ExperimentsPanel />
      </SlidePanel>
      <SlidePanel open={activePanel === "data"} onClose={() => setActivePanel(null)} title="数据中心">
        <DataPanel />
      </SlidePanel>
      <SlidePanel open={activePanel === "system"} onClose={() => setActivePanel(null)} title="系统管理">
        <SystemPanel />
      </SlidePanel>
      <SlidePanel open={activePanel === "risk"} onClose={() => setActivePanel(null)} title="风控中心">
        <RiskPanel />
      </SlidePanel>
      <SlidePanel open={activePanel === "backtest"} onClose={() => setActivePanel(null)} title="回测中心">
        <BacktestPanel />
      </SlidePanel>
    </div>
  );
}

/* ── 近期开仓卡片 (因子管道通过闸门的历史) ── */
function RecentTicksCard() {
  const [ticks, setTicks] = useState<any[]>([]);

  const refresh = useCallback(async () => {
    try {
      const r = await authFetch("/api/v4/recent-ticks?n=15");
      if (r.ok) setTicks(await r.json());
    } catch { /* best-effort */ }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 8000);
    return () => clearInterval(t);
  }, [refresh]);

  const hasData = ticks.length > 0;

  return (
    <Card title="近期开仓" padding="sm">
      {hasData ? (
        <div className="space-y-0.5">
          <div className="grid grid-cols-[3.5rem_3.5rem_3rem_1fr] gap-1 text-2xs text-text-secondary mb-1.5 px-1">
            <span>时间</span>
            <span className="text-right">价格</span>
            <span className="text-center">方向</span>
            <span className="text-left">闸门</span>
          </div>
          {ticks.slice(0, 10).map((t: any, i: number) => {
            const dir = t.signal?.direction ? (t.signal.direction === 1 ? "LONG" : t.signal.direction === -1 ? "SHORT" : "--") : "--";
            const gr = t.gate_result;
            let gate = "--";
            let gateColor = "bg-neutral/10 text-neutral";
            if (gr) {
              if (gr.passed) {
                gate = "通过";
                gateColor = "bg-success/10 text-success";
              } else if (gr.reason?.startsWith("加仓被拦")) {
                gate = gr.reason || "加仓被拦";
                gateColor = "bg-warning/15 text-warning";
              } else {
                const reasonMap: Record<string, string> = {
                  "signal_below_threshold": "信号不足",
                  "cooldown_1": "冷却 1",
                  "cooldown_2": "冷却 2",
                  "nfp_skip": "NFP 事件",
                  "gvz": "GVZ 闸门",
                  "var_gate": "VaR 超限",
                  "risk_gate": "风控拦截",
                  "macd_reverse": "MACD 反向",
                  "event_filter": "事件过滤",
                  "cooldown": "冷却中",
                };
                gate = reasonMap[gr.reason] || gr.reason || "阻挡";
                gateColor = "bg-warning/10 text-warning";
              }
            }
            const price = t.price || "--";
            const ts = t.ts ? new Date(t.ts * 1000).toLocaleTimeString() : "--";
            return (
              <div key={i} className="grid grid-cols-[3.5rem_3.5rem_3rem_1fr] gap-1 items-center py-1 px-1 text-xs border-b border-apple-divider last:border-0 rounded hover:bg-apple-bg/40">
                <span className="text-text-tertiary text-2xs">{ts}</span>
                <span className="num text-text-primary text-right">{typeof price === 'number' ? price.toFixed(2) : price}</span>
                <span className={`text-center font-semibold ${dir === "LONG" ? "text-success" : dir === "SHORT" ? "text-danger" : "text-text-tertiary"}`}>
                  {dir}
                </span>
                <span className={`text-xs px-1.5 py-0.5 rounded font-medium truncate ${gateColor}`}>
                  {gate}
                </span>
              </div>
            );
          })}
          <div className="text-2xs text-text-tertiary text-center pt-1.5">
            每 8s 刷新 · 因子管道信号与闸门判定结果
          </div>
        </div>
      ) : (
        <div className="text-sm text-text-secondary py-4 text-center">启动实盘后显示因子通过闸门的历史 tick</div>
      )}
    </Card>
  );
}

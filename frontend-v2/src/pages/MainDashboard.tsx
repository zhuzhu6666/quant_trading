import { useState, useEffect, useCallback } from "react";
import { useAppStore } from "@/lib/store";
import { fmtNum, fmtPct, fmtUSD } from "@/lib/format";
import { getWSClient } from "@/lib/ws";
import { authFetch } from "@/lib/auth";
import { GlassCard } from "@/components/dashboard/GlassCard";
import { KpiCard } from "@/components/dashboard/KpiCard";
import { DualRing } from "@/components/dashboard/DualRing";
import { MiniAreaChart } from "@/components/dashboard/MiniAreaChart";
import { ProgressBar } from "@/components/dashboard/ProgressBar";
import { FunctionButton } from "@/components/dashboard/FunctionButton";
import { SlidePanel } from "@/components/dashboard/SlidePanel";
import TradingPanel from "@/components/panels/TradingPanel";
import FactorsPanel from "@/components/panels/FactorsPanel";
import ExperimentsPanel from "@/components/panels/ExperimentsPanel";
import DataPanel from "@/components/panels/DataPanel";
import SystemPanel from "@/components/panels/SystemPanel";

type PanelType = "trading" | "factors" | "experiments" | "data" | "system" | null;

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

/* ─── Evolution story (from /api/evolution/latest) ─── */
interface EvoEvent {
  ts: number;
  event_type: string;
  [key: string]: any;
}

const JOB_LABELS: Record<string, string> = {
  evolution_hourly: "自进化循环 (GP→OOS→Canary→退役→权重)",
  canary_fast: "Canary 快速检查",
  retire_hourly: "因子退役检查",
  sync_health: "数据同步健康",
  data_pull: "MT5 数据拉取",
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

  // ── Live loop status ──
  const [loopBusy, setLoopBusy] = useState(false);
  const [loopStatus, setLoopStatus] = useState<{ running: boolean; broker: string | null; started_at: number | null; pid: number | null }>(
    { running: false, broker: null, started_at: null, pid: null }
  );
  const [loopMsg, setLoopMsg] = useState<string | null>(null);
  const [broker] = useState<"mt5" | "ctrader">("ctrader");

  const refreshLoop = useCallback(async () => {
    try {
      const r = await authFetch("/api/live/loop-status");
      if (r.ok) setLoopStatus(await r.json());
    } catch { /* best-effort */ }
  }, []);

  useEffect(() => {
    try { getWSClient().start("/ws/state"); return () => getWSClient().stop(); }
    catch (e) { setErr(String(e)); }
  }, []);

  useEffect(() => { refreshLoop(); const t = setInterval(refreshLoop, 5000); return () => clearInterval(t); }, [refreshLoop]);

  async function startLoop() {
    setLoopBusy(true); setLoopMsg(null);
    try {
      const ar = await authFetch(`/api/live/account?broker=${broker}`);
      if (!ar.ok) { setLoopMsg(`✗ ${broker} 账户预飞失败: HTTP ${ar.status}`); return; }
      const ad = await ar.json();
      if (!ad.ok) { setLoopMsg(`✗ ${broker} 不可达: ${ad.error || "unknown"}`); return; }
      const r = await authFetch("/api/live/start", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ broker, strategy_name: "v1_minimal_ma_cross" }) });
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
      const d = await r.json();
      setLoopMsg(d.was_running ? "✓ loop stopped" : "loop 原本未运行");
      refreshLoop();
    } catch (e: any) { setLoopMsg(`✗ ${e?.message ?? e}`); }
    finally { setLoopBusy(false); }
  }

  async function emergencyClose() {
    if (!window.confirm(`确认紧急平仓 ${broker} 所有持仓? (后端 X-Confirm))`)) return;
    setLoopBusy(true); setLoopMsg(null);
    try {
      const r = await authFetch("/api/live/emergency-close", { method: "POST", headers: { "Content-Type": "application/json", "X-Confirm": "emergency" }, body: JSON.stringify({ broker, symbol: null }) });
      const d = await r.json();
      setLoopMsg(d.ok ? `✓ ${d.broker} 已平` : `✗ ${d.error || "failed"}`);
    } catch (e: any) { setLoopMsg(`✗ ${e?.message ?? e}`); }
    finally { setLoopBusy(false); }
  }

  const s = snapshot;
  const equityHistory = useAppStore((st) => st.equityHistory);
  const equityData = equityHistory.map((p) => p.v);
  const pnl = s?.pnl_today ?? 0;
  const equity = s?.equity ?? 1000;
  const balance = s?.balance ?? 1000;
  const dir = s?.position?.dir ?? "FLAT";
  const trades = s?.daily?.trades ?? 0;
  const wins = s?.daily?.win ?? 0;
  const losses = s?.daily?.loss ?? 0;
  const winRate = trades > 0 ? Math.round((wins / trades) * 100) : 0;
  const dd = s?.daily?.drawdown_pct ?? 0;
  const circuit = s?.risk?.circuit_breaker ?? false;
  const consecLoss = s?.risk?.consecutive_loss ?? 0;

  const source: "live" | "paper" | "none" = (s?.source as any) ?? (loopStatus.running ? "live" : "none");
  const sourceColor: Record<string, { bg: string; fg: string }> = {
    live: { bg: "rgba(220, 38, 38, 0.12)", fg: "#dc2626" },
    paper: { bg: "rgba(217, 119, 6, 0.12)", fg: "#d97706" },
    none: { bg: "rgba(122, 127, 138, 0.12)", fg: "#7a7f8a" },
  };

  const panels: { key: PanelType; icon: string; label: string; desc: string; gradient: "blue" | "green" | "amber" | "purple" | "slate" }[] = [
    { key: "trading", icon: "💹", label: "交易", desc: "模拟盘 · 实盘 · 风控", gradient: "blue" },
    { key: "factors", icon: "🔬", label: "因子", desc: "健康 · 发现 · 影子", gradient: "green" },
    { key: "experiments", icon: "🧪", label: "实验", desc: "调参 · 校准 · A/B", gradient: "amber" },
    { key: "data", icon: "📈", label: "数据", desc: "K线 · 同步", gradient: "purple" },
    { key: "system", icon: "⚙️", label: "系统", desc: "报告 · 配置 · 任务", gradient: "slate" },
  ];

  if (err) {
    return <div className="min-h-screen flex items-center justify-center text-sm text-down">{err}</div>;
  }

  return (
    <div className="min-h-screen flex flex-col" style={{ background: "linear-gradient(135deg, #f5f7fa 0%, #e4e9f0 100%)" }}>
      <div className="max-w-[1600px] mx-auto p-3 md:p-4 flex flex-col flex-1 w-full">

        {/* ── Top Bar ── */}
        <div className="flex items-center justify-between mb-2 pb-2 border-b border-white/30">
          <div className="flex items-center gap-2">
            <span className="text-lg font-bold" style={{ color: "#3b82f6" }}>◆ Quant</span>
            <span className="px-2 py-0.5 rounded text-[10px] font-semibold"
              style={{ background: sourceColor[source]?.bg ?? "#e5e7eb", color: sourceColor[source]?.fg ?? "#6b7280" }}
              data-testid="source-badge">
              {source === "live" ? `● LIVE (${loopStatus.broker ?? "ctrader"})` : source === "paper" ? "● PAPER" : "● 离线"}
            </span>
            {sched?.running && loopStatus.running && (
              <span className="px-2 py-0.5 rounded text-[10px] font-semibold bg-up/10 text-up">
                ● 自主运行
              </span>
            )}
          </div>
          <div className="flex items-center gap-3 text-xs text-fg-muted">
            <span className="px-2 py-0.5 rounded text-xs font-semibold" style={{ background: "#dbeafe", color: "#3b82f6" }}>XAUUSD+</span>
            <span className="text-fg font-semibold num">{fmtNum(equity)}</span>
            <span className={`font-semibold ${pnl >= 0 ? "text-up" : "text-down"}`}>{fmtPct(pnl)}</span>
            {dir !== "FLAT" && <span className={`font-semibold ${dir === "LONG" ? "text-up" : "text-down"}`}>{dir}</span>}
            <span className={`flex items-center gap-1 ${wsConnected ? "text-up" : "text-warn"}`}>
              <span className={`w-1.5 h-1.5 rounded-full ${wsConnected ? "bg-up" : "bg-warn"}`} />
              {wsConnected ? "在线" : "离线"}
            </span>
          </div>
        </div>

        {/* ── KPI Row ── */}
        <div className="grid grid-cols-2 md:grid-cols-6 gap-2 mb-2">
          <div className="md:col-span-1 col-span-2">
            <KpiCard label="账户权益" value={fmtNum(equity)} subvalue={`余额 ${fmtNum(balance)}`} trend="neutral">
              <div className="w-16 h-8"><MiniAreaChart data={equityData} height={28} color="#3b82f6" /></div>
            </KpiCard>
          </div>
          <KpiCard label="今日盈亏" value={fmtUSD(pnl)} subvalue={`交易 ${trades} | 胜 ${wins} | 负 ${losses}`} trend={pnl >= 0 ? "up" : "down"} />
          <div className="flex items-center justify-center">
            <DualRing outerValue={winRate} outerLabel="胜率" innerValue={dd} innerLabel="回撤" size={68} className="scale-[0.85] md:scale-100" />
          </div>
          <KpiCard label="持仓" value={dir} subvalue={s?.position?.dir !== "FLAT" && s ? `@ ${fmtNum(s.position.entry)}` : undefined} trend={dir === "LONG" ? "up" : dir === "SHORT" ? "down" : "neutral"} />
          <KpiCard label="Margin" value={fmtNum(s?.margin ?? 0)} subvalue={`Free ${fmtNum(s?.margin_free ?? 0)}`} trend="neutral" />
          <KpiCard label="风控" value={circuit ? "熔断" : "正常"} subvalue={`连亏 ${consecLoss}`} trend={circuit ? "down" : "neutral"} />
        </div>

        {/* ── Row 2: System Status + Chart + Positions ── */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-2 flex-1 min-h-0">

          {/* ── 自主运行状态 ── */}
          <GlassCard className="p-2.5 flex flex-col">
            <div className="flex items-center justify-between mb-2">
              <span className="text-[11px] text-fg-muted font-semibold">
                {loopStatus.running ? (sched?.running ? "● 自主运行中" : "○ 启动中...") : "○ 等待启动实盘"}
              </span>
              {sched?.error && <span className="text-[9px] text-down">{sched.error}</span>}
            </div>
            <div className="text-[10px] space-y-1.5 flex-1 overflow-y-auto">
              {(sched?.jobs ?? []).map((j) => (
                <div key={j.name} className="flex items-center justify-between">
                  <div className="flex items-center gap-1.5 min-w-0">
                    <span className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${j.error_count > 0 ? "bg-down" : j.run_count > 0 ? "bg-up" : "bg-[#dce0e6]"}`} />
                    <span className="text-fg truncate">{JOB_LABELS[j.name] ?? j.name}</span>
                  </div>
                  <div className="flex items-center gap-2 flex-shrink-0 ml-2">
                    <span className="text-fg-muted">{j.cron_expr}</span>
                    <span className="text-fg-muted font-mono">{j.run_count}次</span>
                    {j.error_count > 0 && <span className="text-down font-mono">{j.error_count}err</span>}
                  </div>
                </div>
              ))}
              {(!sched?.jobs || sched.jobs.length === 0) && (
                <div className="text-fg-muted py-2 text-center">
                  {loopStatus.running ? "调度任务加载中..." : "启动实盘后自动注册 5 个调度任务"}
                </div>
              )}
            </div>

            {/* ── Live Loop 控制 (紧凑) ── */}
            <div className="mt-2 pt-2 border-t border-white/20">
              <div className="flex items-center gap-2">
                <button
                  className="flex-1 py-1 rounded-lg text-[10px] font-semibold text-white transition-all hover:scale-[1.02] disabled:opacity-50"
                  style={{ background: loopStatus.running ? "linear-gradient(135deg, #d97706, #b45309)" : "linear-gradient(135deg, #16a34a, #15803d)" }}
                  onClick={loopStatus.running ? stopLoop : startLoop}
                  disabled={loopBusy}
                  data-testid="main-toggle-loop"
                >
                  {loopBusy ? "..." : loopStatus.running ? "⏹ 停止实盘" : "▶ 启动 cTrader 实盘"}
                </button>
                <button
                  className="py-1 px-2 rounded-lg text-[10px] font-semibold text-white transition-all hover:scale-[1.02]"
                  style={{ background: "linear-gradient(135deg, #dc2626, #b91c1c)" }}
                  onClick={emergencyClose}
                  disabled={loopBusy}
                  data-testid="main-emergency-close"
                >
                  ⏮ 紧急
                </button>
              </div>
              {loopMsg && (
                <div className={`text-[9px] mt-1 ${loopMsg.startsWith("✓") ? "text-up" : "text-down"}`} data-testid="main-loop-msg">
                  {loopMsg}
                </div>
              )}
              <div className="text-[9px] text-fg-muted mt-0.5">
                loop: {loopStatus.running ? `running (${loopStatus.broker} pid ${loopStatus.pid})` : "stopped"}
                {sched?.running && loopStatus.running ? " · 自进化已启动，每小时整点运行" : ""}
              </div>
            </div>
          </GlassCard>

          {/* Chart */}
          <GlassCard className="p-2.5 flex flex-col">
            <div className="flex items-center justify-between mb-2">
              <span className="text-[11px] text-fg-muted font-semibold">权益曲线</span>
              <div className="flex gap-1">
                {["1D", "1W", "1M"].map((t) => (
                  <span key={t} className={`px-2 py-0.5 rounded text-[10px] cursor-pointer transition-colors ${t === "1D" ? "bg-accent/10 text-accent font-semibold" : "text-fg-muted hover:text-fg"}`}>{t}</span>
                ))}
              </div>
            </div>
            <div className="flex-1 min-h-0 flex items-stretch">
              <MiniAreaChart data={equityData} height={72} color="#3b82f6" className="w-full" />
            </div>
          </GlassCard>

          {/* ── Status Summary ── */}
          <GlassCard className="p-2.5 flex flex-col">
            <div className="text-[11px] text-fg-muted font-semibold mb-2">系统总览</div>
            <div className="flex-1 space-y-1.5 text-[10px]">
              <div className="flex justify-between">
                <span className="text-fg-muted">后端</span>
                <span className="font-semibold text-up">运行中</span>
              </div>
              <div className="flex justify-between">
                <span className="text-fg-muted">调度器</span>
                <span className={`font-semibold ${loopStatus.running ? "text-up" : "text-fg-muted"}`}>
                  {loopStatus.running ? `${sched?.jobs?.length ?? 0} 个任务` : "等待实盘"}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-fg-muted">数据同步</span>
                <span className={`font-semibold ${loopStatus.running ? "text-up" : "text-fg-muted"}`}>
                  {loopStatus.running ? "每 10 分钟" : "停盘中"}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-fg-muted">因子发现</span>
                <span className={`font-semibold ${loopStatus.running ? "text-up" : "text-fg-muted"}`}>
                  {loopStatus.running ? "每小时整点" : "停盘中"}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-fg-muted">因子退役</span>
                <span className={`font-semibold ${loopStatus.running ? "text-up" : "text-fg-muted"}`}>
                  {loopStatus.running ? "每小时" : "停盘中"}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-fg-muted">Canary 检查</span>
                <span className={`font-semibold ${loopStatus.running ? "text-up" : "text-fg-muted"}`}>
                  {loopStatus.running ? "每 30 分钟" : "停盘中"}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-fg-muted">实盘执行</span>
                <span className={`font-semibold ${loopStatus.running ? "text-up" : "text-warn"}`}>
                  {loopStatus.running ? `● ${loopStatus.broker}` : "停止"}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-fg-muted">WS</span>
                <span className={`font-semibold ${wsConnected ? "text-up" : "text-warn"}`}>{wsConnected ? "已连接" : "断开"}</span>
              </div>
              <div className="mt-2 pt-2 border-t border-white/20">
                <div className="text-[9px] text-fg-muted">熔断: {circuit ? "触发" : "关闭"} · 连亏: {consecLoss} · 杠杆: {s?.leverage ?? "--"}:1</div>
              </div>
              <div className="text-[9px] text-fg-muted mt-1 italic">
                {loopStatus.running
                  ? sched?.running ? "系统已启动自进化循环，每小时整点自动运行。停止实盘后调度自动终止。" : "调度任务加载中..."
                  : "启动实盘后自进化调度器自动激活。"}
              </div>
            </div>
          </GlassCard>
        </div>

        {/* ── Function Buttons ── */}
        <div className="mt-auto pt-2">
          <div className="text-[10px] text-fg-muted uppercase tracking-wider mb-2">详情面板</div>
          <div className="grid grid-cols-5 gap-2">
            {panels.map((p) => (
              <FunctionButton key={p.key} icon={p.icon} label={p.label} description={p.desc} gradient={p.gradient} onClick={() => setActivePanel(p.key)} />
            ))}
          </div>
        </div>
      </div>

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
    </div>
  );
}

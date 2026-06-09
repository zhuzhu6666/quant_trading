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
type Broker = "mt5" | "ctrader";

const equityData = Array.from({ length: 20 }, (_, i) => 1000 + Math.sin(i * 0.5) * 50 + i * 10);

export default function MainDashboard() {
  const { snapshot, wsConnected } = useAppStore();
  const [activePanel, setActivePanel] = useState<PanelType>(null);
  const [err, setErr] = useState<string | null>(null);

  // B9+B10 fix: 顶部 LIVE/PAPER/OFFLINE 角标 + 真接 live trading loop
  const [broker, setBroker] = useState<Broker>("mt5");
  const [loopBusy, setLoopBusy] = useState(false);
  const [loopStatus, setLoopStatus] = useState<{ running: boolean; broker: string | null; started_at: number | null; pid: number | null }>(
    { running: false, broker: null, started_at: null, pid: null }
  );
  const [loopMsg, setLoopMsg] = useState<string | null>(null);

  const refreshLoop = useCallback(async () => {
    try {
      const r = await authFetch("/api/live/loop-status");
      if (r.ok) setLoopStatus(await r.json());
    } catch {
      // best-effort
    }
  }, []);

  useEffect(() => {
    try { getWSClient().start("/ws/state"); return () => getWSClient().stop(); }
    catch (e) { setErr(String(e)); }
  }, []);

  // 5s 自动刷 loop status
  useEffect(() => {
    refreshLoop();
    const t = setInterval(refreshLoop, 5000);
    return () => clearInterval(t);
  }, [refreshLoop]);

  async function startLoop() {
    setLoopBusy(true);
    setLoopMsg(null);
    try {
      // 先预飞: 调 /account 看 broker 真能连
      const ar = await authFetch(`/api/live/account?broker=${broker}`);
      if (!ar.ok) {
        setLoopMsg(`✗ ${broker} 账户预飞失败: HTTP ${ar.status}`);
        return;
      }
      const ad = await ar.json();
      if (!ad.ok) {
        setLoopMsg(`✗ ${broker} 不可达: ${ad.error || "unknown"}`);
        return;
      }
      const r = await authFetch("/api/live/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ broker, strategy_name: "v1_minimal_ma_cross" }),
      });
      if (!r.ok) {
        setLoopMsg(`✗ 启动失败: HTTP ${r.status}`);
        return;
      }
      const d = await r.json();
      setLoopMsg(`✓ ${d.broker} loop started (pid ${d.pid})`);
      refreshLoop();
    } catch (e: any) {
      setLoopMsg(`✗ ${e?.message ?? e}`);
    } finally {
      setLoopBusy(false);
    }
  }

  async function stopLoop() {
    setLoopBusy(true);
    setLoopMsg(null);
    try {
      const r = await authFetch("/api/live/stop", { method: "POST" });
      const d = await r.json();
      setLoopMsg(d.was_running ? `✓ loop stopped` : `loop 原本未运行`);
      refreshLoop();
    } catch (e: any) {
      setLoopMsg(`✗ ${e?.message ?? e}`);
    } finally {
      setLoopBusy(false);
    }
  }

  async function emergencyClose() {
    if (!window.confirm(`确认紧急平仓 ${broker} 所有持仓? (后端会校验 X-Confirm: emergency header)`)) return;
    setLoopBusy(true);
    setLoopMsg(null);
    try {
      const r = await authFetch("/api/live/emergency-close", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Confirm": "emergency" },
        body: JSON.stringify({ broker, symbol: null }),
      });
      const d = await r.json();
      setLoopMsg(d.ok ? `✓ ${d.broker} ${d.symbol} 已平` : `✗ ${d.error || "failed"}`);
    } catch (e: any) {
      setLoopMsg(`✗ ${e?.message ?? e}`);
    } finally {
      setLoopBusy(false);
    }
  }

  const s = snapshot;
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

  // 角标: source 优先级 snapshot.source > loopStatus.running > "none"
  const source: "live" | "paper" | "none" = (s?.source as any) ?? (loopStatus.running ? "live" : "none");
  const sourceLabel: Record<"live" | "paper" | "none", string> = {
    live: `● LIVE (${loopStatus.broker ?? s?.broker ?? broker})`,
    paper: "● PAPER",
    none: "● 离线",
  };
  const sourceColor: Record<"live" | "paper" | "none", { bg: string; fg: string }> = {
    live: { bg: "rgba(220, 38, 38, 0.12)", fg: "#dc2626" },     // 红
    paper: { bg: "rgba(217, 119, 6, 0.12)", fg: "#d97706" },   // 橙
    none: { bg: "rgba(122, 127, 138, 0.12)", fg: "#7a7f8a" },  // 灰
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
    <div className="min-h-screen" style={{ background: "linear-gradient(135deg, #f5f7fa 0%, #e4e9f0 100%)" }}>
      <div className="max-w-[1200px] mx-auto p-4 md:p-5">

        {/* Top Bar */}
        <div className="flex items-center justify-between mb-4 pb-3 border-b border-white/30">
          <div className="flex items-center gap-2">
            <span className="text-lg font-bold" style={{ color: "#3b82f6" }}>◆ Quant</span>
            {/* B9 fix: LIVE/PAPER/OFFLINE 角标, 之前 source/isLive 拿了不用 */}
            <span
              className="px-2 py-0.5 rounded text-[10px] font-semibold"
              style={{ background: sourceColor[source].bg, color: sourceColor[source].fg }}
              data-testid="source-badge"
            >
              {sourceLabel[source]}
            </span>
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

        {/* === KPI Row === */}
        <div className="grid grid-cols-2 md:grid-cols-6 gap-2.5 mb-3">
          <div className="md:col-span-1 col-span-2">
            <KpiCard label="账户权益" value={fmtNum(equity)} subvalue={`余额 ${fmtNum(balance)}`} trend="neutral">
              <div className="w-16 h-8">
                <MiniAreaChart data={equityData} height={28} color="#3b82f6" />
              </div>
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

        {/* === Row 2: Chart + Positions + Risk === */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-2.5 mb-4">
          {/* Chart */}
          <GlassCard className="p-3.5">
            <div className="flex items-center justify-between mb-2">
              <span className="text-[11px] text-fg-muted font-semibold">权益曲线</span>
              <div className="flex gap-1">
                {["1D", "1W", "1M"].map((t) => (
                  <span key={t} className={`px-2 py-0.5 rounded text-[10px] cursor-pointer transition-colors ${t === "1D" ? "bg-accent/10 text-accent font-semibold" : "text-fg-muted hover:text-fg"}`}>{t}</span>
                ))}
              </div>
            </div>
            <div className="h-20">
              <MiniAreaChart data={equityData.map((v, i) => v + Math.random() * 20 - 10)} height={72} color="#3b82f6" />
            </div>
          </GlassCard>

          {/* Positions */}
          <GlassCard className="p-3.5">
            <div className="text-[11px] text-fg-muted font-semibold mb-3">持仓 ({s?.n_positions ?? 0})</div>
            {s?.n_positions && s.n_positions > 0 ? (
              <div className="space-y-3">
                <div>
                  <div className="flex justify-between text-[10px] mb-1">
                    <span className="font-semibold text-fg">XAUUSD</span>
                    <span className="text-up font-semibold">+12.40</span>
                  </div>
                  <ProgressBar value={65} color="#16a34a" />
                  <div className="text-[9px] text-fg-muted mt-0.5">LONG × 0.1 @ 1,985</div>
                </div>
                <div>
                  <div className="flex justify-between text-[10px] mb-1">
                    <span className="font-semibold text-fg">BTCUSD</span>
                    <span className="text-up font-semibold">+5.20</span>
                  </div>
                  <ProgressBar value={25} color="#3b82f6" />
                  <div className="text-[9px] text-fg-muted mt-0.5">SHORT × 0.01 @ 98,450</div>
                </div>
              </div>
            ) : (
              <div className="text-xs text-fg-muted py-4 text-center">无持仓</div>
            )}
          </GlassCard>

          {/* Risk / Quick Actions */}
          <GlassCard className="p-3.5">
            <div className="text-[11px] text-fg-muted font-semibold mb-3">快速操作</div>
            <div className="space-y-2">
              <div className="flex justify-between text-[10px]">
                <span className="text-fg-muted">熔断</span>
                <span className={`px-1.5 rounded text-[10px] font-semibold ${circuit ? "bg-down/10 text-down" : "bg-up/10 text-up"}`}>{circuit ? "触发" : "关闭"}</span>
              </div>
              <div className="flex justify-between text-[10px]">
                <span className="text-fg-muted">连续亏损</span>
                <span className="font-semibold" style={{ color: consecLoss > 3 ? "#dc2626" : consecLoss > 1 ? "#d97706" : "#1a1e24" }}>{consecLoss} 次</span>
              </div>
              <div className="flex justify-between text-[10px]">
                <span className="text-fg-muted">杠杆</span>
                <span className="font-semibold text-fg">{s?.leverage ?? "--"}:1</span>
              </div>
              <div className="mt-2 pt-2 border-t border-white/20">
                <div className="text-[10px] text-fg-muted mb-1">风险敞口</div>
                <ProgressBar value={(s?.margin && s?.equity ? (s.margin / s.equity) * 100 : 0)} color="#d97706" />
              </div>
              <div className="flex gap-2 mt-3">
                {/* B10 fix: ▶ 启动 / ⏹ 停止 接真 /api/live/start /stop, ⏮ 紧急平仓 接 /api/live/emergency-close */}
                <button
                  className="flex-1 py-1.5 rounded-lg text-xs font-semibold text-white transition-all duration-200 hover:scale-[1.02] active:scale-[0.98] disabled:opacity-50 disabled:cursor-not-allowed"
                  style={{ background: "linear-gradient(135deg, #16a34a, #15803d)" }}
                  onClick={startLoop}
                  disabled={loopBusy || loopStatus.running}
                  data-testid="main-start-loop"
                >
                  {loopStatus.running ? `▶ 运行中` : `▶ 启动 ${broker.toUpperCase()}`}
                </button>
                <button
                  className="flex-1 py-1.5 rounded-lg text-xs font-semibold text-white transition-all duration-200 hover:scale-[1.02] active:scale-[0.98] disabled:opacity-50 disabled:cursor-not-allowed"
                  style={{ background: "linear-gradient(135deg, #d97706, #b45309)" }}
                  onClick={stopLoop}
                  disabled={loopBusy || !loopStatus.running}
                  data-testid="main-stop-loop"
                >
                  ⏹ 停止
                </button>
                <button
                  className="flex-1 py-1.5 rounded-lg text-xs font-semibold text-white transition-all duration-200 hover:scale-[1.02] active:scale-[0.98] disabled:opacity-50 disabled:cursor-not-allowed"
                  style={{ background: "linear-gradient(135deg, #dc2626, #b91c1c)" }}
                  onClick={emergencyClose}
                  disabled={loopBusy}
                  data-testid="main-emergency-close"
                >
                  ⏮ 紧急
                </button>
              </div>
              {/* broker 切换 + 状态 + msg */}
              <div className="mt-2 pt-2 border-t border-white/20 space-y-1">
                <div className="flex gap-1">
                  {(["mt5", "ctrader"] as Broker[]).map((b) => (
                    <button
                      key={b}
                      onClick={() => setBroker(b)}
                      className={`flex-1 py-0.5 rounded text-[10px] font-semibold transition-colors ${broker === b ? "bg-accent/15 text-accent" : "text-fg-muted hover:text-fg"}`}
                    >
                      {b.toUpperCase()}
                    </button>
                  ))}
                </div>
                <div className="text-[10px] text-fg-muted">
                  loop: {loopStatus.running
                    ? `running (${loopStatus.broker} pid ${loopStatus.pid})`
                    : "stopped"}
                </div>
                {loopMsg && (
                  <div className={`text-[10px] ${loopMsg.startsWith("✓") ? "text-up" : "text-down"}`} data-testid="main-loop-msg">
                    {loopMsg}
                  </div>
                )}
              </div>
            </div>
          </GlassCard>
        </div>

        {/* === Function Buttons === */}
        <div>
          <div className="text-[10px] text-fg-muted uppercase tracking-wider mb-2.5">功能模块</div>
          <div className="grid grid-cols-5 gap-2.5">
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

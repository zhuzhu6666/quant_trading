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
import OpsPanel from "@/components/panels/OpsPanel";
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
  canary_fast: "Canary 快速检查",
  retire_hourly: "因子退役检查",
  sync_health: "数据同步健康",
  data_pull: "数据拉取 (MT5→DataStore)",
  awe_adapt: "AWE 权重自适应 (每30分钟)",
};

const CRON_LABELS: Record<string, string> = {
  "0 * * * *": "每小时整点",
  "*/30 * * * *": "每 30 分钟",
  "10 * * * *": "每小时第 10 分",
  "*/5 * * * *": "每 5 分钟",
  "*/10 * * * *": "每 10 分钟",
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
            .sort((a, b) => b.weight - a.weight)
            .slice(0, 10);
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
      if (!r.ok) { setLoopMsg(`✗ 停止失败: HTTP ${r.status}`); return; }
      const d = await r.json();
      setLoopMsg(d.was_running ? "✓ loop stopped" : "loop 原本未运行");
      refreshLoop();
    } catch (e: any) { setLoopMsg(`✗ ${e?.message ?? e}`); }
    finally { setLoopBusy(false); }
  }

  async function emergencyClose() {
    if (!window.confirm(`确认紧急平仓 ${broker} 所有持仓? (后端 X-Confirm)`)) return;
    setLoopBusy(true); setLoopMsg(null);
    try {
      const r = await authFetch("/api/live/emergency-close", { method: "POST", headers: { "Content-Type": "application/json", "X-Confirm": "emergency" }, body: JSON.stringify({ broker, symbol: null }) });
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

  const panels: { key: PanelType; icon: string; label: string; desc: string; accent: string }[] = [
    { key: "trading", icon: "💹", label: "交易", desc: "模拟盘 · 实盘 · 风控", accent: "#0071E3" },
    { key: "factors", icon: "🔬", label: "因子", desc: "健康 · 发现 · 影子", accent: "#34C759" },
    { key: "experiments", icon: "🧪", label: "实验", desc: "调参 · 校准 · A/B", accent: "#FF9500" },
    { key: "data", icon: "📈", label: "数据", desc: "K线 · 外部数据 · 同步", accent: "#AF52DE" },
    { key: "system", icon: "⚙️", label: "系统", desc: "报告 · 配置 · 任务", accent: "#5856D6" },
    { key: "risk", icon: "🛡️", label: "风控", desc: "VaR · Kelly · 压力测试", accent: "#FF3B30" },
    { key: "ops", icon: "🚨", label: "运维", desc: "告警 · 恢复 · 周报", accent: "#FF2D55" },
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
              <span className={`w-2 h-2 rounded-full ${wsConnected ? "bg-success" : "bg-warning"} ${wsConnected ? "animate-pulse-soft" : ""}`} />
              <span className={`text-xs ${wsConnected ? "text-success" : "text-warning"}`}>
                {wsConnected ? "已连接" : "断开"}
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
                innerValue={Math.round(dd)}
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
              label="Margin"
              value={fmtNum(s?.margin ?? 0)}
              subvalue={`Free ${fmtNum(s?.margin_free ?? 0)}`}
              trend="neutral"
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
                    <span className={`w-1.5 h-1.5 rounded-full ${wsConnected ? "bg-success" : "bg-warning"}`} />
                    <span className={wsConnected ? "text-success" : "text-warning"}>
                      {wsConnected ? "已连接" : "断开"}
                    </span>
                  </div>
                </div>
              </div>

              {/* Scheduler Jobs */}
              <div className="flex-1 overflow-y-auto min-h-0 space-y-1">
                {(sched?.jobs ?? []).map((j) => (
                  <div key={j.name} className="flex items-center justify-between py-1.5 border-b border-apple-divider last:border-0">
                    <div className="flex items-center gap-2.5 min-w-0">
                      <span className={`w-2 h-2 rounded-full flex-shrink-0 ${
                        j.error_count > 0 ? "bg-danger" : j.run_count > 0 ? "bg-success" : "bg-text-tertiary"
                      }`} />
                      <span className="text-xs text-text-primary truncate" title={JOB_LABELS[j.name] ?? j.name}>
                        {JOB_LABELS[j.name] ?? j.name}
                      </span>
                    </div>
                    <div className="flex items-center gap-3 flex-shrink-0 ml-2 text-2xs">
                      <span className="text-text-secondary" title={j.cron_expr}>
                        {CRON_LABELS[j.cron_expr] ?? j.cron_expr}
                      </span>
                      <span className="text-text-secondary">
                        已执行 <span className="font-semibold text-text-primary">{j.run_count}</span> 次
                      </span>
                      {j.error_count > 0 && (
                        <span className="text-danger font-semibold">{j.error_count} 次错误</span>
                      )}
                    </div>
                  </div>
                ))}
                {(!sched?.jobs || sched.jobs.length === 0) && (
                  <div className="text-xs text-text-secondary py-4 text-center">
                    {loopStatus.running ? "调度任务加载中..." : "启动实盘后自动注册 7 个调度任务"}
                  </div>
                )}
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

            {/* ── 因子管道 (活跃因子权重 + 闸门状态) ── */}
            <Card title="因子管道" padding="sm">
              {factorWeights.length > 0 ? (
                <div className="space-y-1">
                  <div className="flex items-center justify-between text-2xs text-text-secondary mb-2 px-1">
                    <span>因子</span>
                    <span>权重  |  闸门</span>
                  </div>
                  {factorWeights.slice(0, 10).map((fw) => (
                    <div key={fw.factor} className="flex items-center justify-between py-1 px-1 text-xs border-b border-apple-divider last:border-0 rounded hover:bg-apple-bg/40">
                      <span className="text-text-primary truncate mr-2 max-w-[160px]" title={fw.factor}>{fw.factor}</span>
                      <div className="flex items-center gap-2 flex-shrink-0">
                        <span className="font-semibold num text-text-primary w-12 text-right">{fw.weight.toFixed(3)}</span>
                        <span className={`text-2xs px-1.5 py-0.5 rounded font-medium ${
                          fw.weight > 0.05 ? "bg-success/10 text-success" : "bg-text-tertiary/10 text-text-tertiary"
                        }`}>
                          {fw.weight > 0.05 ? "通过" : "休眠"}
                        </span>
                      </div>
                    </div>
                  ))}
                  <div className="text-2xs text-text-tertiary text-center pt-1">
                    AWE 每 30 分钟自适应更新权重
                  </div>
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
      <SlidePanel open={activePanel === "ops"} onClose={() => setActivePanel(null)} title="运维中心">
        <OpsPanel />
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
          <div className="flex items-center justify-between text-2xs text-text-secondary mb-1.5 px-1">
            <span>时间</span>
            <span>价格  | 方向  | 闸门</span>
          </div>
          {ticks.slice(0, 10).map((t: any, i: number) => {
            const dir = t.signal?.direction ? (t.signal.direction === 1 ? "LONG" : t.signal.direction === -1 ? "SHORT" : "--") : "--";
            const gate = t.gate_result ? (t.gate_result.passed ? "通过" : t.gate_result.reason || "阻挡") : "--";
            const price = t.price || "--";
            const ts = t.ts ? new Date(t.ts * 1000).toLocaleTimeString() : "--";
            return (
              <div key={i} className="flex items-center justify-between py-1 px-1 text-xs border-b border-apple-divider last:border-0 rounded hover:bg-apple-bg/40">
                <span className="text-text-tertiary text-2xs w-14 flex-shrink-0">{ts}</span>
                <span className="num text-text-primary w-14 text-right">{typeof price === 'number' ? price.toFixed(2) : price}</span>
                <span className={`w-12 text-center font-semibold ${dir === "LONG" ? "text-success" : dir === "SHORT" ? "text-danger" : "text-text-tertiary"}`}>
                  {dir}
                </span>
                <span className={`text-2xs px-1.5 py-0.5 rounded font-medium ${
                  gate === "通过" ? "bg-success/10 text-success" : "bg-warning/10 text-warning"
                }`}>
                  {gate}
                </span>
              </div>
            );
          })}
          <div className="text-2xs text-text-tertiary text-center pt-1.5">
            每 8s 刷新 · 显示最近通过 ExecutionGate 的因子信号
          </div>
        </div>
      ) : (
        <div className="text-sm text-text-secondary py-4 text-center">启动实盘后显示因子通过闸门的历史 tick</div>
      )}
    </Card>
  );
}

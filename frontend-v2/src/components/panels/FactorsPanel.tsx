import { useEffect, useState, useCallback } from "react";
import { Link } from "react-router-dom";
import { authFetch } from "@/lib/auth";
import { Button, Badge, Card, Table, ProgressBar, Input, TabBar } from "@/components/ui";
import type { Column } from "@/components/ui";
import { useApi, useJobPolling } from "@/lib/hooks";

const FACTOR_TABS = [
  { key: "factors" as const, label: "因子健康" },
  { key: "discover" as const, label: "因子发现" },
  { key: "shadow" as const, label: "影子因子" },
  { key: "ml" as const, label: "ML因子" },
];
type Tab = (typeof FACTOR_TABS)[number]["key"];

/* ─── Types ─── */
interface Factor { factor: string; score: number; status: "HEALTHY" | "WATCH" | "DECAYING"; components: { mean_abs_ic: number; ic_stability: number; regime_consistency: number; decay_rate: number; independence: number }; n_obs: number; rolling_ic: number; }
interface Report { factors: Factor[]; healthy: number; watch: number; decaying: number; unknown?: number; dead?: number; total?: number; }
interface TopFactor { name: string; expr: string; ic: number; }
interface Shadow { name: string; status?: string; source?: string; ts?: string; expr?: string; description?: string; }
interface ShadowResponse { shadows: Shadow[]; }

function flat(f: Factor) { return { name: f.factor, status: f.status, score: f.score, abs_ic: f.components.mean_abs_ic, stability: f.components.ic_stability, decay: f.components.decay_rate, regime_consistency: f.components.regime_consistency, independence: f.components.independence }; }
type FlatFactor = ReturnType<typeof flat>;

export default function FactorsPanel() {
  const [tab, setTab] = useState<Tab>("factors");
  return (
    <div className="space-y-4">
      <TabBar tabs={FACTOR_TABS} active={tab} onChange={(k) => setTab(k as Tab)} />
      {tab === "factors" && <FactorsContent />}
      {tab === "discover" && <DiscoverContent />}
      {tab === "shadow" && <ShadowContent />}
      {tab === "ml" && <MLContent />}
    </div>
  );
}

/* ==================================================================
   Factors (因子健康) — 自动调度状态
   ================================================================== */
function FactorsContent() {
  const { data: reportRaw, loading, refresh } = useApi<Report | { report: Report }>("/api/factor-health/latest");
  const report = (reportRaw as any)?.report ?? (reportRaw as Report);
  const [schedRunning, setSchedRunning] = useState(false);
  const [lastEvo, setLastEvo] = useState<string | null>(null);

  useEffect(() => {
    authFetch("/api/control/scheduler").then(r => r.ok ? r.json().then(d => setSchedRunning(d.running)) : undefined).catch(() => {});
    // Try to get last evolution event
    authFetch("/api/control/evolution/latest").then(r => r.ok ? r.json().then(d => setLastEvo(d?.ts_iso ?? null)) : undefined).catch(() => {});
  }, []);

  const factors: FlatFactor[] = report ? report.factors.map(flat) : [];

  const columns: Column<FlatFactor>[] = [
    { key: "name", header: "名称", render: (f) => (<Link to={`/factors/${encodeURIComponent(f.name)}`} className="text-accent hover:underline">{f.name}</Link>) },
    { key: "status", header: "状态", render: (f) => (<Badge variant={f.status === "HEALTHY" ? "success" : f.status === "WATCH" ? "warning" : f.status === "DECAYING" || f.status === "DEAD" ? "danger" : "default"}>{f.status}</Badge>) },
    { key: "score", header: "得分", align: "right", render: (f) => <>{Number.isFinite(f.score) ? f.score.toFixed(1) : "--"}</> },
    { key: "abs_ic", header: "abs IC", align: "right", render: (f) => <>{Number.isFinite(f.abs_ic) ? f.abs_ic.toFixed(4) : "--"}</> },
    { key: "stability", header: "stability", align: "right", render: (f) => <>{Number.isFinite(f.stability) ? f.stability.toFixed(2) : "--"}</> },
    { key: "regime_consistency", header: "regime", align: "right", render: (f) => <>{Number.isFinite(f.regime_consistency) ? f.regime_consistency.toFixed(2) : "--"}</> },
  ];

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">因子健康</h1>
        <Button variant="ghost" size="sm" onClick={refresh}>刷新</Button>
      </div>

      {schedRunning && (
        <Card padding="sm">
          <div className="text-xs text-up flex items-center gap-2">
            <span className="w-1.5 h-1.5 rounded-full bg-up" />
            自动调度: 因子健康评估由 evolution_hourly (每小时整点) 自动运行
          </div>
        </Card>
      )}

      <div className="flex items-center gap-4 flex-wrap">
        {report && (
          <div className="flex gap-2 flex-wrap">
            <span className="text-xs text-fg-muted self-center">{report.total ?? report.factors.length} 总</span>
            <Badge variant="success">{report.healthy} HEALTHY</Badge>
            <Badge variant="warning">{report.watch} WATCH</Badge>
            <Badge variant="danger">{report.decaying} DECAYING</Badge>
            {(() => {
              const dead_count = report.dead ?? ((report.total ?? report.factors.length) - report.healthy - report.watch - report.decaying - (report.unknown ?? 0));
              return dead_count > 0 ? <Badge variant="danger">{dead_count} DEAD</Badge> : null;
            })()}
            {(report.unknown ?? 0) > 0 && <Badge variant="default">{report.unknown} UNKNOWN</Badge>}
          </div>
        )}
        {lastEvo && <span className="text-[10px] text-fg-muted">上次自进化: {new Date(lastEvo).toLocaleString()}</span>}
      </div>

      <Table columns={columns} data={factors} keyExtractor={(f) => f.name} loading={loading} emptyMessage="尚无报告, 等待自动评估。" />
    </div>
  );
}

/* ==================================================================
   Discover (因子发现) — 自动调度状态
   ================================================================== */
function DiscoverContent() {
  const [schedRunning, setSchedRunning] = useState(false);
  const [lastRun, setLastRun] = useState<string | null>(null);
  const [topFactors, setTopFactors] = useState<TopFactor[]>([]);
  const [discoverJobId, setDiscoverJobId] = useState<string | null>(null);
  const [discovering, setDiscovering] = useState(false);

  useEffect(() => {
    authFetch("/api/control/scheduler").then(r => r.ok ? r.json().then(d => setSchedRunning(d.running)) : undefined).catch(() => {});
    // Read latest discovery from reports
    authFetch("/api/reports?kind=json").then(async r => {
      if (!r.ok) return;
      const d = await r.json();
      const reports: any[] = d.reports ?? [];
      const gpReport = reports.filter((r: any) => r.name === "discover_report.json").sort((a: any, b: any) => b.modified_at.localeCompare(a.modified_at))[0];
      if (gpReport) {
        setLastRun(gpReport.modified_at);
        const rr = await authFetch(`/api/reports/${encodeURIComponent(gpReport.name)}`);
        if (rr.ok) {
          const rd = await rr.json();
          const top = rd?.content?.top ?? rd?.top ?? [];
          setTopFactors(top.slice(0, 10).map((s: any) => ({ name: s.name ?? s.expression?.slice(0, 30), expr: s.expression ?? "", ic: s.score ?? s.ic ?? 0 })));
        }
      }
    }).catch(() => {});
  }, []);

  async function runManualDiscover() {
    setDiscovering(true);
    setDiscoverJobId(null);
    try {
      const r = await authFetch("/api/discover", { method: "POST" });
      if (r.ok) {
        const d = await r.json();
        setDiscoverJobId(d.job_id ?? d.id ?? JSON.stringify(d));
      } else {
        const err = await r.json().catch(() => ({}));
        setDiscoverJobId(`错误 (${r.status}): ${err.detail?.msg ?? r.statusText}`);
      }
    } catch (e: any) {
      setDiscoverJobId(`请求失败: ${e.message}`);
    } finally {
      setDiscovering(false);
    }
  }

  const columns: Column<TopFactor>[] = [
    { key: "rank", header: "#", render: (_, i) => <span className="text-fg-muted">{i + 1}</span>, width: "48px" },
    { key: "name", header: "name", render: (f) => <span className="text-fg">{f.name}</span> },
    { key: "ic", header: "IC", render: (f) => <span className="num">{f.ic.toFixed(4)}</span>, align: "right" },
    { key: "expr", header: "expr", render: (f) => <span className="text-fg-muted truncate max-w-md">{f.expr}</span> },
  ];

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">L2 因子发现 (自动)</h1>

      {schedRunning && (
        <Card padding="sm">
          <div className="flex items-center gap-2 text-xs text-up">
            <span className="w-1.5 h-1.5 rounded-full bg-up" />
            自动调度: GP 因子发现每小时整点执行 (pop=50, gen=20)。
            评估、晋升/回滚、退役均由 evolution_hourly 自动完成。
          </div>
        </Card>
      )}

      {lastRun && (
        <div className="flex items-center gap-2">
          <Badge variant="info">上次 GP 运行</Badge>
          <span className="text-xs text-fg-muted">{new Date(lastRun).toLocaleString()}</span>
          <Button variant="ghost" size="sm" onClick={() => window.location.reload()}>刷新</Button>
        </div>
      )}

      {topFactors.length > 0 && (
        <Card>
          <div className="text-sm text-fg-muted mb-2">最近一次发现结果 (Top 10)</div>
          <Table columns={columns} data={topFactors} keyExtractor={(f, i) => f.name + i} />
        </Card>
      )}

      {!lastRun && (
        <Card padding="sm">
          <div className="text-xs text-fg-muted">尚未有 GP 运行记录。首次启动实盘后每小时整点自动运行。</div>
        </Card>
      )}

      <div className="flex items-center gap-3">
        <Button onClick={runManualDiscover} disabled={discovering} loading={discovering}>
          手动发现
        </Button>
        {discoverJobId && (
          <span className="text-xs text-fg-muted">job_id: {discoverJobId}</span>
        )}
      </div>
    </div>
  );
}

/* ==================================================================
   Shadow (影子因子) — 自动 Canary 管理
   ================================================================== */
function ShadowContent() {
  const { data, loading, refresh } = useApi<ShadowResponse>("/api/shadow");
  const shadows = data?.shadows ?? [];
  const [actionMsg, setActionMsg] = useState<string | null>(null);

  async function promote(name: string) {
    setActionMsg(null);
    try {
      const r = await authFetch("/api/shadow/promote", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      const d = r.ok ? await r.json() : await r.json().catch(() => ({}));
      setActionMsg(r.ok ? `✓ ${name} promoted` : `✗ ${name}: ${d.detail?.msg ?? r.statusText}`);
      refresh();
    } catch (e: any) {
      setActionMsg(`✗ ${name}: ${e.message}`);
    }
  }

  async function demote(name: string) {
    setActionMsg(null);
    try {
      const r = await authFetch("/api/shadow/demote", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      const d = r.ok ? await r.json() : await r.json().catch(() => ({}));
      setActionMsg(r.ok ? `✓ ${name} demoted` : `✗ ${name}: ${d.detail?.msg ?? r.statusText}`);
      refresh();
    } catch (e: any) {
      setActionMsg(`✗ ${name}: ${e.message}`);
    }
  }

  const columns: Column<Shadow>[] = [
    { key: "name", header: "名称", render: (s) => <span style={{ color: "#e6edf3" }}>{s.name}</span> },
    { key: "status", header: "状态", render: (s) => <Badge variant={s.status === "discovered" || s.status === "active" ? "success" : "warning"}>{s.status ?? "--"}</Badge> },
    { key: "expr", header: "表达式", render: (s) => <span className="text-fg-muted truncate max-w-md" title={s.expr}>{s.expr || "--"}</span> },
    { key: "ts", header: "注册时间", align: "right", render: (s) => s.ts ? new Date(s.ts).toLocaleString() : "--" },
    {
      key: "actions", header: "操作", width: "160px",
      render: (s) => (
        <span className="flex gap-1">
          <Button variant="ghost" size="sm" onClick={() => promote(s.name)}>promote</Button>
          <Button variant="ghost" size="sm" onClick={() => demote(s.name)}>demote</Button>
        </span>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">影子因子</h1>
        <Button variant="ghost" size="sm" onClick={refresh}>刷新</Button>
      </div>
      <Card padding="sm" className="border-info/30">
        <p className="text-xs text-info">
          影子因子由 CanaryDirector 自动管理: SHADOW → CANARY_5 → CANARY_20 → CANARY_50 → ACTIVE。
          Promotion/rollback 由 canary_fast (每 30 分钟) 自动评估。无需手动 promote/demote。
        </p>
      </Card>
      {actionMsg && (
        <div className={`text-xs ${actionMsg.startsWith("✓") ? "text-up" : "text-down"}`}>{actionMsg}</div>
      )}
      <Table columns={columns} data={shadows} keyExtractor={(s) => s.name} loading={loading} emptyMessage="暂无影子因子" />
    </div>
  );
}

/* ==================================================================
   ML 因子 (Phase 2)
   ================================================================== */
function MLContent() {
  const { data: reportRaw, loading, refresh } = useApi<Report | { report: Report }>("/api/factor-health/latest");
  const report = (reportRaw as any)?.report ?? (reportRaw as Report);
  const [weights, setWeights] = useState<Record<string, number>>({});
  const [driftStatus, setDriftStatus] = useState<string>("--");

  // ML 手动训练状态
  const [trainStatus, setTrainStatus] = useState<"idle" | "training" | "done" | "error">("idle");
  const [trainMsg, setTrainMsg] = useState<string>("");

  // 筛选 ML 因子 (前缀 xgb_ 或 ml_)
  const mlFactors = report?.factors?.filter((f: Factor) =>
    f.factor.startsWith("xgb_") || f.factor.startsWith("ml_") || f.factor.startsWith("lgb_")
  ) || [];
  const xgbFactor = mlFactors.find((f: Factor) => f.factor === "xgb_dir");

  // 训练中每 2s 轮询刷新数据
  useEffect(() => {
    if (trainStatus !== "training") return;
    const poll = setInterval(refresh, 2000);
    return () => clearInterval(poll);
  }, [trainStatus, refresh]);

  // 训练超时
  useEffect(() => {
    if (trainStatus !== "training") return;
    const timeout = setTimeout(() => {
      setTrainStatus("error");
      setTrainMsg("训练超时，请检查后端日志");
    }, 90000);
    return () => clearTimeout(timeout);
  }, [trainStatus]);

  const handleTrain = useCallback(async () => {
    setTrainStatus("training");
    setTrainMsg("");
    try {
      const r = await authFetch("/api/v4/ml/retrain", { method: "POST" });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        throw new Error(d.detail?.msg || d.detail || r.statusText);
      }
      setTrainStatus("done");
      setTrainMsg("");
      refresh();
    } catch (e: any) {
      setTrainStatus("error");
      setTrainMsg(e.message);
    }
  }, []);

  useEffect(() => {
    // 获取 v4 weights (含 xgb_dir 权重)
    authFetch("/api/v4/weights").then(async r => {
      if (!r.ok) return;
      const data = await r.json();
      if (Array.isArray(data)) {
        const m: Record<string, number> = {};
        for (const entry of data) {
          if (entry.factor && entry.new !== undefined) {
            m[entry.factor] = entry.new;
          }
        }
        setWeights(m);
      }
    }).catch(() => {});
    // 定期刷新
    const t = setInterval(refresh, 30000);
    return () => clearInterval(t);
  }, [refresh]);

  return (
    <div className="space-y-3">
      <div className="text-xs text-fg-muted">
        {loading ? "加载中..." : `ML 因子: ${mlFactors.length} 个 (共 ${report?.factors?.length || 0} 因子)`}
      </div>

      {/* XGBoost 方向预测器 */}
      {xgbFactor ? (
        <Card className="p-3 space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-[#1a1e24]">{xgbFactor.factor}</span>
            <Badge variant={xgbFactor.status === "HEALTHY" ? "success" : xgbFactor.status === "WATCH" ? "warning" : "danger"}>
              {xgbFactor.status}
            </Badge>
          </div>
          <div className="grid grid-cols-2 gap-2 text-[11px]">
            <div><span className="text-fg-muted">健康分:</span> <span className="font-mono text-[#1a1e24]">{xgbFactor.score.toFixed(1)}</span></div>
            <div><span className="text-fg-muted">IC:</span> <span className="font-mono text-[#1a1e24]">{xgbFactor.rolling_ic?.toFixed(4) ?? "--"}</span></div>
            <div><span className="text-fg-muted">权重:</span> <span className="font-mono text-[#1a1e24]">{weights["xgb_dir"]?.toFixed(3) ?? "--"}</span></div>
            <div><span className="text-fg-muted">观察数:</span> <span className="font-mono text-[#1a1e24]">{xgbFactor.n_obs}</span></div>
            <div><span className="text-fg-muted">稳定性:</span> <span className="font-mono text-[#1a1e24]">{xgbFactor.components.ic_stability.toFixed(0)}</span></div>
            <div><span className="text-fg-muted">独立性:</span> <span className="font-mono text-[#1a1e24]">{xgbFactor.components.independence.toFixed(0)}</span></div>
          </div>
        </Card>
      ) : (
        <Card className="p-3">
          <div className="flex flex-col items-center gap-3 py-2">
            {trainStatus === "idle" ? (
              loading ? (
                <div className="text-[11px] text-fg-muted">加载中...</div>
              ) : (
                <Button variant="primary" size="sm" onClick={handleTrain}>
                  手动训练 XGBoost
                </Button>
              )
            ) : trainStatus === "training" ? (
              <div className="flex items-center gap-2 text-[11px] text-fg-muted">
                <svg className="animate-spin h-3 w-3" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
                </svg>
                训练中...
              </div>
            ) : trainStatus === "done" ? (
              <div className="flex items-center gap-2 text-[11px] text-up">
                ✓ 训练完成
              </div>
            ) : (
              <div className="flex flex-col items-center gap-2">
                <div className="text-[11px] text-down">{trainMsg}</div>
                <Button variant="ghost" size="sm" onClick={() => setTrainStatus("idle")}>
                  重试
                </Button>
              </div>
            )}
          </div>
        </Card>
      )}

      {/* 其他 ML 因子 */}
      {mlFactors.filter((f: Factor) => f.factor !== "xgb_dir").map((f: Factor) => (
        <Card key={f.factor} className="p-2 flex items-center justify-between">
          <span className="text-[11px] font-mono text-[#1a1e24]">{f.factor}</span>
          <Badge variant={f.status === "HEALTHY" ? "success" : "warning"}>{f.status}</Badge>
        </Card>
      ))}

      {/* 信息提示 */}
      <div className="text-[10px] text-fg-muted leading-relaxed mt-2 pt-2 border-t border-[#dce0e6]">
        ML 因子与手工因子走同一管道: 归一化 → 组合 → 归因 → 自适应。<br />
        训练: XGBoost (n=200, depth=4), PurgedWalkForward 5-fold, OOS acc &gt; 0.51 + CI &gt; 0.5 才注册。<br />
        重训: 每周日 05:00 UTC · 漂移检测: 每 6 小时。
      </div>
    </div>
  );
}


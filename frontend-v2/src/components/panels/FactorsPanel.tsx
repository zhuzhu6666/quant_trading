import { useEffect, useState, useCallback } from "react";
import { Link } from "react-router-dom";
import { authFetch } from "@/lib/auth";
import { Button, Badge, Card, Table, ProgressBar, Input } from "@/components/ui";
import type { Column } from "@/components/ui";
import { useApi, useJobPolling } from "@/lib/hooks";

/* ─── Types ─── */
interface Factor { factor: string; score: number; status: "HEALTHY" | "WATCH" | "DECAYING"; components: { mean_abs_ic: number; ic_stability: number; regime_consistency: number; decay_rate: number; independence: number }; n_obs: number; rolling_ic: number; }
interface Report { factors: Factor[]; healthy: number; watch: number; decaying: number; unknown?: number; dead?: number; total?: number; }
interface TopFactor { name: string; expr: string; ic: number; }
interface Shadow { name: string; status?: string; action?: string; ts?: string; expr?: string; ic?: number; cv_score?: number; }
interface ShadowResponse { shadows: Shadow[]; }

function flat(f: Factor) { return { name: f.factor, status: f.status, score: f.score, abs_ic: f.components.mean_abs_ic, stability: f.components.ic_stability, decay: f.components.decay_rate, regime_consistency: f.components.regime_consistency, independence: f.components.independence }; }
type FlatFactor = ReturnType<typeof flat>;

const tabs = ["factors", "discover", "shadow"] as const;
type Tab = (typeof tabs)[number];

export default function FactorsPanel() {
  const [tab, setTab] = useState<Tab>("factors");
  return (
    <div className="space-y-4">
      <div className="flex gap-1 mb-4 p-1 rounded-lg" style={{ background: "rgba(255,255,255,0.5)" }}>
        {tabs.map((t) => (
          <button key={t} onClick={() => setTab(t)}
            className={`flex-1 py-1.5 px-3 rounded-md text-xs font-medium transition-all duration-200 ${tab === t ? "bg-[#d4edda] text-[#1a1e24]" : "bg-[#dce0e6] text-[#4a4f59] hover:bg-[#d0d5dd] hover:text-[#1a1e24]"}`}
          >{t === "factors" ? "因子健康" : t === "discover" ? "因子发现" : "影子因子"}</button>
        ))}
      </div>
      {tab === "factors" && <FactorsContent />}
      {tab === "discover" && <DiscoverContent />}
      {tab === "shadow" && <ShadowContent />}
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
            自动调度: 因子健康评估由 evolution_hourly (每小时整点) + canary_fast (每 30 分钟) 自动运行
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

  useEffect(() => {
    authFetch("/api/control/scheduler").then(r => r.ok ? r.json().then(d => setSchedRunning(d.running)) : undefined).catch(() => {});
    // Read latest discovery from reports
    authFetch("/api/reports?kind=json").then(async r => {
      if (!r.ok) return;
      const d = await r.json();
      const reports: any[] = d.reports ?? [];
      const gpReport = reports.filter((r: any) => r.name.startsWith("gp_run")).sort((a: any, b: any) => b.modified_at.localeCompare(a.modified_at))[0];
      if (gpReport) {
        setLastRun(gpReport.modified_at);
        const rr = await authFetch(`/api/reports/${encodeURIComponent(gpReport.name)}`);
        if (rr.ok) {
          const rd = await rr.json();
          const top = rd?.content?.result?.best ?? rd?.result?.best ?? [];
          setTopFactors(top.slice(0, 10).map((s: any) => ({ name: s.name ?? s.expression?.slice(0, 30), expr: s.expression ?? "", ic: s.score ?? s.ic ?? 0 })));
        }
      }
    }).catch(() => {});
  }, []);

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
            Canary 评估每 30 分钟检查晋升/回滚。
            因子退役每小时检查。
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
    </div>
  );
}

/* ==================================================================
   Shadow (影子因子) — 自动 Canary 管理
   ================================================================== */
function ShadowContent() {
  const { data, loading, refresh } = useApi<ShadowResponse>("/api/shadow");
  const shadows = data?.shadows ?? [];

  const columns: Column<Shadow>[] = [
    { key: "name", header: "name", render: (s) => <span style={{ color: "#e6edf3" }}>{s.name}</span> },
    { key: "status", header: "status", render: (s) => <Badge variant={s.status === "active" ? "success" : "warning"}>{s.status ?? "--"}</Badge> },
    { key: "action", header: "last action", render: (s) => <span style={{ color: "#8b949e" }}>{s.action ?? "--"}</span> },
    { key: "ic", header: "IC", align: "right", render: (s) => (s.ic ?? 0).toFixed(4) },
    { key: "ts", header: "timestamp", align: "right", render: (s) => s.ts ? new Date(s.ts).toLocaleTimeString() : "--" },
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
      <Table columns={columns} data={shadows} keyExtractor={(s) => s.name} loading={loading} emptyMessage="暂无影子因子" />
    </div>
  );
}

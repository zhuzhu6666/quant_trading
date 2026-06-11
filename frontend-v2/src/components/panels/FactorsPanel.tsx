import { useEffect, useState, useCallback } from "react";
import { Link } from "react-router-dom";
import { authFetch } from "@/lib/auth";
import { Button, Badge, Card, Table, ProgressBar, Input } from "@/components/ui";
import type { Column } from "@/components/ui";
import { useApi, useJobPolling } from "@/lib/hooks";

/* ───────── Factor health types ───────── */
interface Factor {
  factor: string;
  score: number;
  status: "HEALTHY" | "WATCH" | "DECAYING";
  components: {
    mean_abs_ic: number;
    ic_stability: number;
    regime_consistency: number;
    decay_rate: number;
    independence: number;
  };
  n_obs: number;
  rolling_ic: number;
}

interface Report {
  factors: Factor[];
  healthy: number;
  watch: number;
  decaying: number;
}

function flat(f: Factor) {
  return {
    name: f.factor,
    status: f.status,
    score: f.score,
    abs_ic: f.components.mean_abs_ic,
    stability: f.components.ic_stability,
    decay: f.components.decay_rate,
    regime_consistency: f.components.regime_consistency,
    independence: f.components.independence,
  };
}

type FlatFactor = ReturnType<typeof flat>;

/* ───────── Discover types ───────── */
interface TopFactor {
  name: string;
  expr: string;
  ic: number;
}

/* ───────── Shadow types ───────── */
interface Shadow {
  name: string;
  status?: string;
  action?: string;
  ts?: string;
  expr?: string;
  ic?: number;
  cv_score?: number;
}

interface ShadowResponse {
  shadows: Shadow[];
}

const tabs = ["factors", "discover", "shadow"] as const;
type Tab = (typeof tabs)[number];

export default function FactorsPanel() {
  const [tab, setTab] = useState<Tab>("factors");

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
            {t === "factors" ? "因子健康" : t === "discover" ? "因子发现" : "影子因子"}
          </button>
        ))}
      </div>

      {tab === "factors" && <FactorsContent />}
      {tab === "discover" && <DiscoverContent />}
      {tab === "shadow" && <ShadowContent />}
    </div>
  );
}

/* ==================================================================
   Factors (因子健康) tab
   ================================================================== */
function FactorsContent() {
  // B4 fix: 后端 /api/factor-health/latest 返 {report: {...}, report_path: "..."},
  // 兼容两种 shape (旧版本直接返 Report) — 自动解包
  const { data: reportRaw, loading, refresh } = useApi<Report | { report: Report }>(
    "/api/factor-health/latest"
  );
  const report = (reportRaw as any)?.report ?? (reportRaw as Report);
  const { done, start, cancel } = useJobPolling(
    (id: string) => `/api/jobs/${id}`
  );
  const [running, setRunning] = useState(false);

  useEffect(() => {
    return () => cancel();
  }, [cancel]);

  useEffect(() => {
    if (done) {
      setRunning(false);
      refresh();
    }
  }, [done, refresh]);

  const handleReevaluate = useCallback(async () => {
    setRunning(true);
    try {
      const r = await authFetch("/api/factor-health/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ threshold: 0.04, bar_count: 50000, sync_run: false }),
      });
      const d = await r.json();
      if (d.job_id) {
        start(d.job_id);
      } else {
        setTimeout(() => refresh(), 30000);
      }
    } catch {
      setRunning(false);
    }
  }, [start, refresh]);

  const factors: FlatFactor[] = report
    ? report.factors.slice(0, 50).map(flat)
    : [];

  const columns: Column<FlatFactor>[] = [
    {
      key: "name",
      header: "名称",
      render: (f) => (
        <Link
          to={`/factors/${encodeURIComponent(f.name)}`}
          className="text-accent hover:underline"
        >
          {f.name}
        </Link>
      ),
    },
    {
      key: "status",
      header: "状态",
      render: (f) => (
        <Badge
          variant={
            f.status === "HEALTHY"
              ? "success"
              : f.status === "WATCH"
                ? "warning"
                : "default"
                }
                >
                {f.status}
        </Badge>
      ),
    },
    {
      key: "score",
      header: "得分",
      align: "right",
      render: (f) => (
        <>{Number.isFinite(f.score) ? f.score.toFixed(1) : "--"}</>
      ),
    },
    {
      key: "abs_ic",
      header: "abs IC",
      align: "right",
      render: (f) => (
        <>{Number.isFinite(f.abs_ic) ? f.abs_ic.toFixed(4) : "--"}</>
      ),
    },
    {
      key: "stability",
      header: "stability",
      align: "right",
      render: (f) => (
        <>{Number.isFinite(f.stability) ? f.stability.toFixed(2) : "--"}</>
      ),
    },
    {
      key: "regime_consistency",
      header: "regime",
      align: "right",
      render: (f) => (
        <>
          {Number.isFinite(f.regime_consistency)
            ? f.regime_consistency.toFixed(2)
            : "--"}
        </>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">因子健康</h1>
      <div className="flex items-center gap-4">
        <Button variant="primary" onClick={handleReevaluate} loading={running}>
          ▶ 重新评估
        </Button>
        {report && (
          <div className="flex gap-2 flex-wrap">
            <Badge variant="success">{report.healthy} HEALTHY</Badge>
            <Badge variant="warning">{report.watch} WATCH</Badge>
            <Badge variant="danger">{report.decaying} DECAYING</Badge>
            {(() => { const n = report.factors?.filter((f: any) => !['HEALTHY','WATCH','DECAYING'].includes(f.status)).length; return n > 0 ? <Badge variant="default">{n} UNKNOWN</Badge> : null })()}
          </div>
        )}
      </div>
      <Table
        columns={columns}
        data={factors}
        keyExtractor={(f) => f.name}
        loading={loading}
        emptyMessage='尚无报告，点击"重新评估"生成。'
      />
    </div>
  );
}

/* ==================================================================
   Discover (因子发现) tab
   ================================================================== */
function DiscoverContent() {
  const [engine, setEngine] = useState<"gp" | "random">("gp");
  const [nCandidates, setNCandidates] = useState(1000);
  const [topK, setTopK] = useState(50);
  const [forwardPeriods, setForwardPeriods] = useState("1,5,20");
  const [autoRegister, setAutoRegister] = useState(true);
  const [gpPop, setGpPop] = useState(100);
  const [gpGen, setGpGen] = useState(20);
  const [jobId, setJobId] = useState<string | null>(null);

  const poller = useJobPolling((id: string) => `/api/discover/${id}`);
  const isRunning =
    poller.progress?.status === "running" ||
    poller.progress?.status === "queued";

  const topFactors: TopFactor[] = poller.result?.top_factors ?? [];

  async function start() {
    const r = await authFetch("/api/discover", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        engine,
        n_candidates: nCandidates,
        top_k: topK,
        forward_periods: forwardPeriods
          .split(",")
          .map((s) => parseInt(s.trim()))
          .filter((n) => !isNaN(n)),
        auto_register: autoRegister,
        gp_pop: gpPop,
        gp_gen: gpGen,
      }),
    });
    if (!r.ok) return;
    const d = await r.json();
    setJobId(d.job_id);
    poller.start(d.job_id);
  }

  const columns: Column<TopFactor>[] = [
    {
      key: "rank",
      header: "#",
      render: (_, i) => (
        <span className="text-fg-muted">{i + 1}</span>
      ),
      width: "48px",
    },
    {
      key: "name",
      header: "name",
      render: (f) => <span className="text-fg">{f.name}</span>,
    },
    {
      key: "ic",
      header: "IC",
      render: (f) => (
        <span className="num">{f.ic.toFixed(4)}</span>
      ),
      align: "right",
    },
    {
      key: "expr",
      header: "expr",
      render: (f) => (
        <span className="text-fg-muted truncate max-w-md">
          {f.expr}
        </span>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">L2 因子发现</h1>

      <Card>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          {/* Engine selector */}
          <div className="md:col-span-3 inline-flex gap-4">
            <label className="flex items-center gap-2 cursor-pointer text-sm">
              <input
                type="radio"
                name="engine"
                checked={engine === "gp"}
                onChange={() => setEngine("gp")}
              />
              <span>GP (推荐)</span>
            </label>
            <label className="flex items-center gap-2 cursor-pointer text-sm">
              <input
                type="radio"
                name="engine"
                checked={engine === "random"}
                onChange={() => setEngine("random")}
              />
              <span>Random</span>
            </label>
          </div>

          <Input
            label="n_candidates"
            type="number"
            value={nCandidates}
            onChange={(e) =>
              setNCandidates(parseInt(e.target.value) || 0)
            }
          />
          <Input
            label="top_k"
            type="number"
            value={topK}
            onChange={(e) => setTopK(parseInt(e.target.value) || 0)}
          />
          <Input
            label="forward_periods (csv)"
            type="text"
            value={forwardPeriods}
            onChange={(e) => setForwardPeriods(e.target.value)}
          />

          {engine === "gp" && (
            <>
              <Input
                label="gp_pop"
                type="number"
                value={gpPop}
                onChange={(e) =>
                  setGpPop(parseInt(e.target.value) || 0)
                }
              />
              <Input
                label="gp_gen"
                type="number"
                value={gpGen}
                onChange={(e) =>
                  setGpGen(parseInt(e.target.value) || 0)
                }
              />
            </>
          )}

          <div className="md:col-span-3 flex items-center gap-2 pt-2">
            <input
              type="checkbox"
              id="autoreg"
              checked={autoRegister}
              onChange={(e) => setAutoRegister(e.target.checked)}
            />
            <label
              htmlFor="autoreg"
              className="cursor-pointer text-sm"
            >
              自动注册为 shadow 因子
            </label>
          </div>
        </div>
      </Card>

      <div className="flex gap-2 items-center">
        <Button onClick={start} disabled={isRunning} loading={isRunning}>
          开始发现
        </Button>
        {jobId && (
          <span className="text-xs text-fg-muted">job: {jobId}</span>
        )}
      </div>

      {poller.progress && (
        <ProgressBar
          pct={poller.progress.pct}
          status={poller.progress.status}
          step={poller.progress.step}
        />
      )}

      {poller.error && (
        <div className="text-sm text-down">{poller.error}</div>
      )}

      {poller.done && topFactors.length > 0 && (
        <Table
          columns={columns}
          data={topFactors}
          keyExtractor={(f) => f.name}
        />
      )}
    </div>
  );
}

/* ==================================================================
   Shadow (影子因子) tab
   ================================================================== */
function ShadowContent() {
  const { data, loading, refresh } = useApi<ShadowResponse>("/api/shadow");
  const [busy, setBusy] = useState<string | null>(null);

  const shadows = data?.shadows ?? [];

  async function act(name: string, action: "promote" | "demote") {
    setBusy(name);
    try {
      await authFetch(`/api/shadow/${action}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      await refresh();
    } finally {
      setBusy(null);
    }
  }

  const columns: Column<Shadow>[] = [
    {
      key: "name",
      header: "name",
      render: (s) => (
        <span style={{ color: "#e6edf3" }}>{s.name}</span>
      ),
    },
    {
      key: "status",
      header: "status",
      render: (s) => (
        <Badge variant={s.status === "active" ? "success" : "warning"}>
          {s.status ?? "--"}
        </Badge>
      ),
    },
    {
      key: "action",
      header: "last action",
      render: (s) => (
        <span style={{ color: "#8b949e" }}>{s.action ?? "--"}</span>
      ),
    },
    {
      key: "ic",
      header: "IC",
      align: "right",
      render: (s) => (s.ic ?? 0).toFixed(4),
    },
    {
      key: "cv",
      header: "CV score",
      align: "right",
      render: (s) => (s.cv_score ?? 0).toFixed(3),
    },
    {
      key: "ts",
      header: "timestamp",
      align: "right",
      render: (s) => (
        <span style={{ color: "#8b949e" }}>{s.ts ?? "--"}</span>
      ),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      render: (s) => (
        <div className="flex gap-1 justify-end">
          <Button
            variant="success"
            size="sm"
            onClick={(e) => {
              e.stopPropagation();
              act(s.name, "promote");
            }}
            disabled={busy === s.name}
          >
            promote
          </Button>
          <Button
            variant="warning"
            size="sm"
            onClick={(e) => {
              e.stopPropagation();
              act(s.name, "demote");
            }}
            disabled={busy === s.name}
          >
            demote
          </Button>
        </div>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1
          className="text-2xl font-bold"
          style={{ color: "#e6edf3" }}
        >
          影子因子
        </h1>
        <button
          onClick={refresh}
          className="text-xs hover:underline"
          style={{ color: "#8b949e" }}
        >
          刷新
        </button>
      </div>
      <Table
        columns={columns}
        data={shadows}
        keyExtractor={(s) => s.name}
        loading={loading}
        emptyMessage="尚无影子因子"
      />
    </div>
  );
}

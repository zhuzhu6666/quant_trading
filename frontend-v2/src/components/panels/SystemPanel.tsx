import { useEffect, useState, useCallback } from "react";
import { Button, Card, Badge, Table, Select, Modal, TabBar } from "@/components/ui";
import type { Column } from "@/components/ui";
import { useApi, usePolling } from "@/lib/hooks";
import { authFetch } from "@/lib/auth";

export default function SystemPanel() {
  const [activeTab, setActiveTab] = useState("reports");

  return (
    <div>
      <TabBar
        tabs={[
          { key: "reports", label: "报告" },
          { key: "config", label: "配置" },
          { key: "jobs", label: "任务" },
          { key: "attribution", label: "归因" },
          { key: "recovery", label: "恢复" },
          { key: "weekly", label: "周报" },
        ]}
        active={activeTab}
        onChange={setActiveTab}
      />

      {activeTab === "reports" && <ReportsSection />}
      {activeTab === "config" && <ConfigSection />}
      {activeTab === "jobs" && <JobsSection />}
      {activeTab === "attribution" && <AttributionSection />}
      {activeTab === "recovery" && <RecoverySection />}
      {activeTab === "weekly" && <WeeklySection />}
    </div>
  );
}

/* ===== 报告 (from Reports.tsx) ===== */
interface ReportEntry {
  name: string;
  path: string;
  kind: string;
  size: number;
  modified_at: string;
}

interface ReportsResponse {
  reports: ReportEntry[];
}

const badgeVariantForKind: Record<string, "default" | "success" | "warning" | "danger" | "info" | "gold"> = {
  json: "info",
  txt: "default",
  png: "gold",
  npy: "warning",
};

const filterOptions = [
  { value: "all", label: "all" },
  { value: "txt", label: "txt" },
  { value: "json", label: "json" },
  { value: "png", label: "png" },
  { value: "npy", label: "npy" },
];

function ReportsSection() {
  const [selected, setSelected] = useState<string | null>(null);
  const [content, setContent] = useState<any>(null);
  const [truncated, setTruncated] = useState(false);
  const [contentKind, setContentKind] = useState<string | null>(null);
  const [filter, setFilter] = useState("all");

  const { data, loading, error, refresh } = useApi<ReportsResponse>(
    `/api/reports?kind=${filter}`
  );

  const reports = data?.reports ?? [];

  async function open(name: string) {
    setSelected(name);
    setContent(null);
    setTruncated(false);
    setContentKind(null);
    const r = await authFetch(`/api/reports/${encodeURIComponent(name)}`);
    if (r.ok) {
      const d = await r.json();
      setContentKind(d.kind ?? null);
      if (d.kind === "png") {
        setContent(d.data_url);
      } else {
        setContent(d.content);
        setTruncated(d.truncated ?? false);
      }
    } else {
      setContent({ error: "read failed" });
    }
  }

  const columns: Column<ReportEntry>[] = [
    {
      key: "name",
      header: "name",
      render: (r) => <span className="text-fg">{r.name}</span>,
    },
    {
      key: "kind",
      header: "kind",
      width: "80px",
      render: (r) => (
        <Badge variant={badgeVariantForKind[r.kind] ?? "default"}>
          {r.kind}
        </Badge>
      ),
    },
    {
      key: "size",
      header: "size",
      align: "right",
      width: "80px",
      render: (r) => `${(r.size / 1024).toFixed(1)}K`,
    },
    {
      key: "modified_at",
      header: "modified",
      align: "right",
      render: (r) => r.modified_at.slice(0, 19),
    },
  ];

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">报告浏览器</h1>

      <div className="flex items-center gap-4">
        <span className="text-sm text-fg-muted">{reports.length} 个文件</span>
        <div className="w-32">
          <Select
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            options={filterOptions}
          />
        </div>
        <Button variant="ghost" size="sm" onClick={refresh}>
          刷新
        </Button>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Table
          columns={columns}
          data={reports}
          keyExtractor={(r) => r.name}
          loading={loading}
          emptyMessage="暂无文件"
          onRowClick={(item) => open(item.name)}
          selectedKey={selected ?? undefined}
          className="max-h-[70vh] overflow-y-auto"
        />

        <Card className="max-h-[70vh] overflow-y-auto">
          {!selected && (
            <div className="text-fg-muted">点击左侧文件名查看内容</div>
          )}
          {selected && content !== null && (
            <div>
              <div className="flex items-center justify-between mb-2">
                <div className="text-sm text-fg-muted">{selected}</div>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setSelected(null);
                    setContent(null);
                  }}
                >
                  关闭
                </Button>
              </div>
              {truncated && (
                <div className="text-warn text-xs mb-2">
                  ⚠ 文件 &gt; 1MB,已截断到末 1MB
                </div>
              )}
              {typeof content === "string" &&
              content.startsWith("data:image/png;base64,") ? (
                // @ts-ignore
                <img src={content} alt={selected} className="max-w-full" />
              ) : typeof content === "string" ? (
                <pre className="text-xs whitespace-pre-wrap num text-fg">
                  {content}
                </pre>
              ) : (
                <pre className="text-xs whitespace-pre-wrap num text-fg">
                  {JSON.stringify(content, null, 2)}
                </pre>
              )}
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}

/* ===== 配置 (from Config.tsx) ===== */
function ConfigSection() {
  const [yamlText, setYamlText] = useState("");
  const [parsed, setParsed] = useState<any>(null);
  const [path, setPath] = useState<string>("");
  const [parseError, setParseError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function load() {
    const r = await authFetch("/api/config");
    const d = await r.json();
    setYamlText(d.yaml);
    setParsed(d.parsed);
    setPath(d.path);
    setParseError(d.parse_error ?? null);
  }

  useEffect(() => {
    load();
  }, []);

  async function save() {
    setBusy(true);
    setSaved(null);
    try {
      const r = await authFetch("/api/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ yaml: yamlText }),
      });
      if (r.status === 422) {
        const e = await r.json();
        setSaved(
          `解析错误: ${e.detail?.error ?? JSON.stringify(e.detail)}`
        );
        return;
      }
      const d = await r.json();
      setSaved(
        `已保存. 变更: ${
          d.changes?.length ? d.changes.join("; ") : "(none)"
        }`
      );
      await load();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">配置 (settings.yaml)</h1>
      <div className="text-xs text-fg-muted font-mono">{path}</div>

      {parseError && (
        <div className="flex items-center gap-2">
          <Badge variant="danger">解析错误</Badge>
          <span className="text-xs text-down">{parseError}</span>
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Card title="YAML">
          <div className="flex items-center justify-end gap-2 mb-2">
            <Button variant="ghost" size="sm" onClick={load}>
              重载
            </Button>
            <Button size="sm" onClick={save} loading={busy}>
              保存
            </Button>
          </div>
          <textarea
            value={yamlText}
            onChange={(e) => setYamlText(e.target.value)}
            rows={24}
            className="w-full bg-white border border-[#dce0e6] rounded p-2 font-mono text-xs num text-[#1a1e24]"
          />
          {saved && (
            <div className="text-xs mt-2 text-fg-muted">{saved}</div>
          )}
        </Card>

        <Card
          title="解析预览"
          className="max-h-[70vh] overflow-y-auto"
        >
          <pre className="text-xs whitespace-pre-wrap num text-fg">
            {parsed
              ? JSON.stringify(parsed, null, 2)
              : "(empty)"}
          </pre>
        </Card>
      </div>
    </div>
  );
}

/* ===== 任务 (from Jobs.tsx) ===== */
interface Job {
  id: string;
  kind: string;
  status: "queued" | "running" | "done" | "error" | "cancelled";
  progress_pct: number;
  current_step: string;
  started_at: string;
  finished_at: string | null;
  params: Record<string, any>;
  result: any;
  error: string | null;
}

const KIND_OPTIONS = [
  { value: "all", label: "all" },
  { value: "backtest", label: "backtest" },
  { value: "factor_health", label: "factor_health" },
  { value: "discover", label: "discover" },
  { value: "sync", label: "sync" },
  { value: "tuning", label: "tuning" },
  { value: "ab_test", label: "ab_test" },
];

const STATUS_OPTIONS = [
  { value: "all", label: "all" },
  { value: "queued", label: "queued" },
  { value: "running", label: "running" },
  { value: "done", label: "done" },
  { value: "error", label: "error" },
  { value: "cancelled", label: "cancelled" },
];

function statusBadgeVariant(
  status: string,
): "success" | "info" | "danger" | "warning" | "default" {
  switch (status) {
    case "done":
      return "success";
    case "running":
      return "info";
    case "error":
      return "danger";
    case "cancelled":
      return "warning";
    default:
      return "default";
  }
}

function JobsSection() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [kindFilter, setKindFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState("all");
  const [selected, setSelected] = useState<Job | null>(null);
  // (audit 2026-06-08: prevent double-click on the cancel button. The
  // first POST transitions the job from running -> cancelled; a second
  // POST would 400 with "job not running or not found", which is harmless
  // but spams the API and the 5s auto-refresh would briefly show a
  // "running" row between the two POSTs, making the button flash.)
  const [cancellingId, setCancellingId] = useState<string | null>(null);

  async function load() {
    const params = new URLSearchParams();
    if (kindFilter !== "all") params.set("kind", kindFilter);
    if (statusFilter !== "all") params.set("status", statusFilter);
    const r = await authFetch(`/api/jobs?${params}`);
    const d = await r.json();
    setJobs(d.jobs || []);
  }

  // Immediate load on filter change
  useEffect(() => { load(); }, [kindFilter, statusFilter]);
  // Auto-refresh every 5s
  usePolling(load, 5000, [kindFilter, statusFilter]);

  async function cancel(id: string) {
    if (cancellingId) return; // already cancelling something
    setCancellingId(id);
    try {
      await authFetch(`/api/jobs/${id}/cancel`, { method: "POST" });
      await load();
    } finally {
      setCancellingId(null);
    }
  }

  async function select(id: string) {
    const r = await authFetch(`/api/jobs/${id}`);
    if (r.ok) setSelected(await r.json());
  }

  const columns: Column<Job>[] = [
    {
      key: "id",
      header: "id",
      align: "left",
      render: (j) => (
        <span className="text-fg-muted font-mono text-xs">
          {j.id.slice(0, 8)}
        </span>
      ),
    },
    {
      key: "kind",
      header: "kind",
      align: "left",
      render: (j) => <span className="text-fg">{j.kind}</span>,
    },
    {
      key: "status",
      header: "status",
      align: "left",
      render: (j) => (
        <Badge variant={statusBadgeVariant(j.status)}>{j.status}</Badge>
      ),
    },
    {
      key: "progress",
      header: "progress",
      align: "right",
      render: (j) => <>{j.progress_pct.toFixed(0)}%</>,
    },
    {
      key: "step",
      header: "step",
      align: "left",
      render: (j) => (
        <span className="text-fg-muted truncate max-w-[200px] inline-block align-middle">
          {j.current_step}
        </span>
      ),
    },
    {
      key: "started",
      header: "started",
      align: "right",
      render: (j) => (
        <span className="text-fg-muted">{j.started_at.slice(11, 19)}</span>
      ),
    },
    {
      key: "actions",
      header: "操作",
      align: "right",
      render: (j) =>
        j.status === "running" ? (
          <Button
            variant="warning"
            size="sm"
            onClick={(e) => {
              e.stopPropagation();
              cancel(j.id);
            }}
            disabled={cancellingId !== null}
            loading={cancellingId === j.id}
          >
            {cancellingId === j.id ? "取消中..." : "cancel"}
          </Button>
        ) : null,
    },
  ];

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">任务中心</h1>

      {/* Filters */}
      <div className="flex items-end gap-4">
        <div className="w-36">
          <Select
            label="类型"
            options={KIND_OPTIONS}
            value={kindFilter}
            onChange={(e) => setKindFilter(e.target.value)}
          />
        </div>
        <div className="w-36">
          <Select
            label="状态"
            options={STATUS_OPTIONS}
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
          />
        </div>
        <Button variant="ghost" size="sm" onClick={load}>
          刷新
        </Button>
        <span className="text-xs text-fg-muted ml-auto">每 5s 自动刷新</span>
      </div>

      {/* Job list table */}
      <Table
        columns={columns}
        data={jobs}
        keyExtractor={(j) => j.id}
        onRowClick={(j) => select(j.id)}
        selectedKey={selected?.id}
        emptyMessage="无任务"
      />

      {/* Job detail modal */}
      <Modal
        open={selected !== null}
        onClose={() => setSelected(null)}
        title={`任务详情: ${selected?.id ?? ""}`}
      >
        <pre className="text-xs whitespace-pre-wrap num text-fg max-h-96 overflow-y-auto">
          {JSON.stringify(selected, null, 2)}
        </pre>
      </Modal>
    </div>
  );
}

/* ===== 归因 (原 MainDashboard 归因概览 + 组合权重) ===== */
function AttributionSection() {
  const [factorWeights, setFactorWeights] = useState<{ factor: string; weight: number }[]>([]);
  const [v4Stats, setV4Stats] = useState<Record<string, any>>({});

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

  const refreshV4Stats = useCallback(async () => {
    try {
      const r = await authFetch("/api/v4/stats");
      if (r.ok) setV4Stats(await r.json());
    } catch { /* best-effort */ }
  }, []);

  useEffect(() => {
    refreshFactorWeights();
    const t = setInterval(refreshFactorWeights, 10000);
    return () => clearInterval(t);
  }, [refreshFactorWeights]);

  useEffect(() => {
    refreshV4Stats();
    const t = setInterval(refreshV4Stats, 10000);
    return () => clearInterval(t);
  }, [refreshV4Stats]);

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">归因概览</h1>

      {/* 归因统计数据 */}
      <Card title="归因统计" padding="sm">
        {Object.keys(v4Stats).length > 0 ? (
          <div className="space-y-2">
            {v4Stats.win_rate !== undefined && (
              <div className="flex items-center justify-between text-sm border-b border-apple-divider pb-2 last:border-0 last:pb-0">
                <span className="text-text-secondary">胜率</span>
                <span className="font-semibold text-text-primary">
                  {typeof v4Stats.win_rate === 'number' ? `${(v4Stats.win_rate * 100).toFixed(1)}%` : v4Stats.win_rate}
                </span>
              </div>
            )}
            {v4Stats.sharpe !== undefined && (
              <div className="flex items-center justify-between text-sm border-b border-apple-divider pb-2 last:border-0 last:pb-0">
                <span className="text-text-secondary">Sharpe</span>
                <span className="font-semibold text-text-primary">
                  {typeof v4Stats.sharpe === 'number' ? v4Stats.sharpe.toFixed(2) : v4Stats.sharpe}
                </span>
              </div>
            )}
            {v4Stats.avg_mc !== undefined && (
              <div className="flex items-center justify-between text-sm border-b border-apple-divider pb-2 last:border-0 last:pb-0">
                <span className="text-text-secondary">平均 MC</span>
                <span className="font-semibold text-text-primary">
                  {typeof v4Stats.avg_mc === 'number' ? v4Stats.avg_mc.toFixed(2) : v4Stats.avg_mc}
                </span>
              </div>
            )}
            {v4Stats.total_factors !== undefined && (
              <div className="flex items-center justify-between text-sm">
                <span className="text-text-secondary">因子总数</span>
                <span className="font-semibold text-text-primary">{v4Stats.total_factors}</span>
              </div>
            )}
          </div>
        ) : (
          <div className="text-sm text-text-secondary py-4 text-center">暂无归因数据，启动实盘后自动累积</div>
        )}
      </Card>

      {/* 组合权重 */}
      <Card title="组合权重 (AWE 自适应)" padding="sm">
        {factorWeights.length > 0 ? (
          <div className="space-y-1">
            <div className="flex items-center justify-between text-2xs text-text-secondary mb-2 px-1">
              <span>因子</span>
              <span>权重  |  变化方向</span>
            </div>
            {factorWeights.map((fw) => (
              <div key={fw.factor} className="flex items-center justify-between py-1.5 px-1 text-xs border-b border-apple-divider last:border-0 rounded hover:bg-apple-bg/40">
                <span className="text-text-primary truncate mr-2 max-w-[240px]" title={fw.factor}>{fw.factor}</span>
                <div className="flex items-center gap-3 flex-shrink-0">
                  <div className="w-24 h-2 bg-apple-divider rounded-full overflow-hidden">
                    <div className="h-full rounded-full" style={{
                      width: `${Math.min(100, fw.weight * 100)}%`,
                      background: fw.weight > 0.1 ? "#34C759" : fw.weight > 0.05 ? "#FF9500" : "#8e8e93"
                    }} />
                  </div>
                  <span className="font-semibold num text-text-primary w-14 text-right">{fw.weight.toFixed(4)}</span>
                </div>
              </div>
            ))}
            <div className="text-2xs text-text-tertiary text-center pt-2">
              权重由 AdaptiveWeightEngine 每 30 分钟自动更新 · 基于 NW-HAC Sharpe
            </div>
          </div>
        ) : (
          <div className="text-sm text-text-secondary py-4 text-center">暂无权重数据</div>
        )}
      </Card>

      {/* 归因设置 */}
      <Card title="归因配置" padding="sm">
        <div className="text-xs text-text-secondary space-y-2">
          <p>AWE 参数 (从 RuntimeConfig 读取):</p>
          <ul className="list-disc pl-4 space-y-1">
            <li>灵敏度: 0.5 (权重更新步长)</li>
            <li>锚点回归: 0.15 (防权重偏离过大)</li>
            <li>最小交易数: 50 (before AWE starts)</li>
            <li>IC 下限: 0.02 (低于此值降权)</li>
            <li>健康分下限: 60 (低于此值退役)</li>
            <li>类型上限: 40% (单类型因子权重上限)</li>
          </ul>
          <p className="mt-2">
            归因引擎: 线性 MC + Gram-Schmidt 正交 · NW-HAC Sharpe 评估
          </p>
        </div>
      </Card>
    </div>
  );
}

/* ===== 恢复 (from OpsPanel) ===== */
function RecoverySection() {
  const [recovery, setRecovery] = useState<any>(null);
  const [recoveryHistory, setRecoveryHistory] = useState<any[]>([]);
  const [showHistory, setShowHistory] = useState(false);

  const fetchRecovery = async () => {
    try {
      const r = await authFetch("/api/ops/recovery");
      if (r.ok) setRecovery(await r.json());
    } catch { /* ignore */ }
  };

  const fetchRecoveryHistory = async () => {
    try {
      const r = await authFetch("/api/ops/recovery/history");
      if (r.ok) setRecoveryHistory(await r.json());
    } catch { /* ignore */ }
  };

  useEffect(() => {
    fetchRecovery();
    const t = setInterval(fetchRecovery, 30000);
    return () => clearInterval(t);
  }, []);

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">AutoRecovery 状态</h1>
      <Card padding="md">
        <div className="flex items-center gap-4 mb-4">
          <span className={`w-2.5 h-2.5 rounded-full ${recovery?.loop_healthy ? "bg-success" : "bg-warning"} animate-pulse-soft`} />
          <span className="text-sm font-medium">{recovery?.running ? "30s 心跳正常" : "未启动"}</span>
        </div>
        <div className="space-y-2 text-sm">
          <div className="flex items-center justify-between"><span className="text-fg-muted">运行中</span><span>{recovery?.running ? "是" : "否"}</span></div>
          <div className="flex items-center justify-between"><span className="text-fg-muted">Loop 健康</span><span>{recovery?.loop_healthy ? "健康" : "异常"}</span></div>
          <div className="flex items-center justify-between"><span className="text-fg-muted">调度健康</span><span>{recovery?.scheduler_healthy ? "健康" : "异常"}</span></div>
          <div className="flex items-center justify-between"><span className="text-fg-muted">失败次数</span><span>{recovery?.failures ?? 0}</span></div>
        </div>
      </Card>
      <div className="flex justify-end">
        <Button variant="ghost" size="sm" onClick={() => { setShowHistory(!showHistory); if (!showHistory) fetchRecoveryHistory(); }}>
          {showHistory ? "收起历史" : "查看历史"}
        </Button>
      </div>
      {showHistory && (
        <Card title="恢复历史" padding="md">
          {recoveryHistory.length > 0 ? (
            <table className="w-full text-sm">
              <thead><tr className="border-b text-fg-muted text-2xs uppercase"><th className="text-left py-2">时间</th><th className="text-left py-2">操作</th><th className="text-left py-2">状态</th></tr></thead>
              <tbody>
                {recoveryHistory.map((item: any, idx: number) => (
                  <tr key={idx} className="border-b last:border-0">
                    <td className="py-2">{item.time ? new Date(item.time * 1000).toLocaleString() : "--"}</td>
                    <td className="py-2">{item.action ?? "--"}</td>
                    <td className="py-2"><Badge variant={item.status === "success" ? "success" : "danger"}>{item.status ?? "--"}</Badge></td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <div className="text-sm text-fg-muted text-center py-4">暂无恢复历史</div>}
        </Card>
      )}
    </div>
  );
}

/* ===== 周报 (from OpsPanel) ===== */
function WeeklySection() {
  const [reports, setReports] = useState<any>(null);
  const [loading, setLoading] = useState(false);

  const fetchReports = async () => {
    try {
      const r = await authFetch("/api/ops/reports/weekly");
      if (r.ok) setReports(await r.json());
    } catch { /* ignore */ }
  };

  const generateReport = async () => {
    try { setLoading(true);
      const r = await authFetch("/api/ops/reports/weekly/generate", { method: "POST" });
      if (r.ok) await fetchReports();
    } catch { /* ignore */ }
    finally { setLoading(false); }
  };

  useEffect(() => { fetchReports(); }, []);

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">周报</h1>
      <Card padding="md">
        <div className="flex items-center justify-between mb-4">
          <div className="text-sm text-fg-muted">已生成: <span className="font-medium">{reports?.count ?? 0} 份</span></div>
          <Button variant="primary" size="sm" onClick={generateReport} loading={loading}>生成周报</Button>
        </div>
        <div className="space-y-2">
          {(reports?.reports ?? []).map((rep: any) => (
            <div key={rep.name} className="flex items-center justify-between py-2 border-b last:border-0">
              <span className="text-sm">{rep.name}</span>
              <span className="text-2xs text-fg-muted">{new Date(rep.modified_at * 1000).toLocaleString()}</span>
            </div>
          ))}
          {(!reports?.reports || reports.reports.length === 0) && <div className="text-sm text-fg-muted text-center py-4">暂无周报</div>}
        </div>
      </Card>
    </div>
  );
}

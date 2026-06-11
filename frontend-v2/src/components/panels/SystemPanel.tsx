import { useEffect, useState } from "react";
import { Button, Card, Badge, Table, Select, Modal } from "@/components/ui";
import type { Column } from "@/components/ui";
import { useApi, usePolling } from "@/lib/hooks";
import { authFetch } from "@/lib/auth";

function TabBar({ tabs, active, onChange }: { tabs: {key:string,label:string}[], active: string, onChange: (k:string)=>void }) {
  return (
    <div className="flex gap-1 mb-4 p-1 rounded-lg" style={{ background: "rgba(255,255,255,0.5)" }}>
      {tabs.map(t => (
        <button key={t.key} onClick={() => onChange(t.key)}
          className={`flex-1 py-1.5 px-3 rounded-md text-xs font-medium transition-all duration-200 ${
            active === t.key ? "bg-[#d4edda] text-[#1a1e24]" : "bg-[#dce0e6] text-[#4a4f59] hover:bg-[#d0d5dd] hover:text-[#1a1e24]"
          }`}>
          {t.label}
        </button>
      ))}
    </div>
  );
}

export default function SystemPanel() {
  const [activeTab, setActiveTab] = useState("reports");

  return (
    <div>
      <TabBar
        tabs={[
          { key: "reports", label: "报告" },
          { key: "config", label: "配置" },
          { key: "jobs", label: "任务" },
        ]}
        active={activeTab}
        onChange={setActiveTab}
      />

      {activeTab === "reports" && <ReportsSection />}
      {activeTab === "config" && <ConfigSection />}
      {activeTab === "jobs" && <JobsSection />}
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

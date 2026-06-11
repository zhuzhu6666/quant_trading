import { useEffect, useState } from "react";
import { authFetch } from "@/lib/auth";
import { CandleBar, Candlestick } from "@/components/charts/Candlestick";
import { Button, Card, Badge, Skeleton, Table } from "@/components/ui";
import type { Column } from "@/components/ui";
import { useApi, clearApiCache } from "@/lib/hooks";

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

export default function DataPanel() {
  const [activeTab, setActiveTab] = useState("market");

  return (
    <div>
      <TabBar
        tabs={[
          { key: "market", label: "K线" },
          { key: "external", label: "外部数据" },
          { key: "sync", label: "同步" },
        ]}
        active={activeTab}
        onChange={setActiveTab}
      />

      {activeTab === "market" && <MarketSection />}
      {activeTab === "external" && <ExternalDataSection />}
      {activeTab === "sync" && <SyncSection />}
    </div>
  );
}

/* ===== K线 (from Market.tsx) ===== */
const TIMEFRAMES = ["M5", "M15", "M30", "H1", "H4", "D1"];

function MarketSection() {
  const [tf, setTf] = useState("M15");
  // audit 2026-06-10: 用 useApi 自动享 30s 客户端缓存 + 自动 AbortController,
  // 切 tf / 切 tab 切回不重复拉. 注: tf 变化必须 reset 缓存, 所以 path 含 tf.
  const { data, loading } = useApi<{ bars: CandleBar[]; total: number; range: any }>(
    `/api/market/bars?symbol=XAUUSD%2B&timeframe=${tf}&limit=500`,
  );
  const bars = data?.bars ?? [];
  const last = bars.length > 0 ? bars[bars.length - 1] : null;

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">K线 / 市场数据</h1>

      {/* Segmented control: inline-flex with no gaps */}
      <div className="inline-flex rounded-lg overflow-hidden border border-border">
        {TIMEFRAMES.map((t) => (
          <Button
            key={t}
            variant={tf === t ? "primary" : "secondary"}
            size="sm"
            onClick={() => setTf(t)}
            className={tf === t ? "rounded-none" : "rounded-none border-0"}
          >
            {t}
          </Button>
        ))}
      </div>

      <Card>
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <Badge variant="default">{tf}</Badge>
            {loading ? (
              <Skeleton variant="text" className="w-20" />
            ) : (
              <Badge variant="default">{bars.length} bars</Badge>
            )}
          </div>
          {loading ? (
            <Skeleton variant="text" className="w-48" />
          ) : last ? (
            <Badge variant="default">
              最新 {new Date(last.t * 1000).toLocaleString()} · 收{" "}
              <span className={last.c >= last.o ? "text-up" : "text-down"}>
                {last.c}
              </span>
            </Badge>
          ) : null}
        </div>
        {bars.length > 0 ? (
          <Candlestick bars={bars} height={480} />
        ) : loading ? (
          <Skeleton variant="card" />
        ) : (
          <div className="text-fg-muted text-center py-12">无数据</div>
        )}
      </Card>
    </div>
  );
}

/* ===== 同步 (from Sync.tsx) ===== */
interface PerTF {
  M5?: { last_sync_utc: string; total_bars: number };
  M15?: { last_sync_utc: string; total_bars: number };
  H1?: { last_sync_utc: string; total_bars: number };
  D1?: { last_sync_utc: string; total_bars: number };
}

interface SyncStatusResponse {
  per_tf: PerTF;
  daemon_running: boolean;
}

interface PerTFResult {
  tf: string;
  pulled: number;
  inserted: number;
  error: string;
}

interface SyncResult {
  job_id?: string;
  status?: string;
  total_inserted?: number;
  per_tf?: PerTFResult[];
  error?: string;
}

interface TFTableItem {
  tf: string;
  last_sync_utc?: string;
  total_bars?: number;
}

const columns: Column<TFTableItem>[] = [
  {
    key: "tf",
    header: "Timeframe",
    render: (item) => <Badge variant="info">{item.tf}</Badge>,
  },
  {
    key: "total_bars",
    header: "Bars",
    align: "right",
    render: (item) => item.total_bars ?? "--",
  },
  {
    key: "last_sync_utc",
    header: "Last sync",
    align: "right",
    render: (item) => item.last_sync_utc ?? "--",
  },
];

function SyncSection() {
  const { data: status, loading, refresh } =
    useApi<SyncStatusResponse>("/api/sync/status");
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<SyncResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function runOnce() {
    setRunning(true);
    setResult(null);
    setError(null);
    try {
      const r = await authFetch("/api/sync/once", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          timeframes: ["M15", "H1", "D1"],
          type: "incremental",
        }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        setError(err.detail?.msg || `HTTP ${r.status}`);
        return;
      }
      const data: SyncResult = await r.json();
      setResult(data);
      setTimeout(refresh, 3000);
    } catch (e) {
      setError(String(e));
    } finally {
      setRunning(false);
    }
  }

  const tfData: TFTableItem[] = status?.per_tf
    ? Object.entries(status.per_tf).map(([tf, info]) => ({
        tf,
        last_sync_utc: (info as any)?.last_sync_utc,
        total_bars: (info as any)?.total_bars,
      }))
    : [];

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">T16 实时数据同步</h1>

      <div className="flex gap-2">
        <Button onClick={runOnce} disabled={running} loading={running}>
          {running ? "提交中..." : "▶ 触发一次同步"}
        </Button>
        <Button variant="secondary" onClick={refresh}>
          刷新
        </Button>
      </div>

      {error && (
        <div className="bg-down/10 border border-down rounded-lg p-3 text-sm text-down">
          同步失败: {error}
        </div>
      )}

      {result && (
        <div className="bg-white border border-[#dce0e6] rounded-lg p-3 text-sm">
          <div className="text-fg-muted">
            Job: <span className="text-fg">{result.job_id}</span>
            {result.total_inserted !== undefined && (
              <span className="ml-4">
                插入:{" "}
                <span className="text-up font-semibold">
                  {result.total_inserted}
                </span>{" "}
                条
              </span>
            )}
          </div>
          {result.per_tf && (
            <div className="mt-2 flex flex-wrap gap-2 text-xs text-fg-muted">
              {result.per_tf.map((item) => (
                <span
                  key={item.tf}
                  className="inline-flex items-center gap-1"
                >
                  <Badge variant="info">{item.tf}</Badge>
                  <span>+{item.inserted} (拉取 {item.pulled})</span>
                  {item.error && (
                    <Badge variant="danger">{item.error}</Badge>
                  )}
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      <Table
        columns={columns}
        data={tfData}
        keyExtractor={(item) => item.tf}
        loading={loading}
        emptyMessage="暂无同步数据"
      />
    </div>
  );
}

/* ===== 外部数据时效 ===== */
interface ExternalSource {
  table: string;
  latest: string;
  stale: boolean;
  note?: string;
}

function ExternalDataSection() {
  const { data, loading, refresh } = useApi<{ sources: ExternalSource[] }>(
    "/api/data/external-status"
  );
  const [refreshing, setRefreshing] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  async function handleRefresh() {
    setRefreshing(true);
    setMsg(null);
    try {
      const r = await authFetch("/api/data/external-refresh", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      const result = await r.json();
      setMsg(result.status === "ok" ? "✅ 刷新完成" : "❌ 失败");
      setTimeout(() => { refresh(); setMsg(null); }, 2000);
    } catch (e) {
      setMsg(`❌ ${String(e)}`);
    } finally {
      setRefreshing(false);
    }
  }

  const sources = data?.sources ?? [];

  const extColumns: Column<ExternalSource>[] = [
    {
      key: "table",
      header: "数据源",
      render: (item) => <Badge variant="info">{item.table}</Badge>,
    },
    {
      key: "latest",
      header: "最新日期",
      align: "right" as const,
      render: (item) => {
        if (item.table === "mt5_bars") return <span className="text-fg-muted">{item.note || "⏸"}</span>;
        return <span>{item.latest}</span>;
      },
    },
    {
      key: "stale",
      header: "状态",
      align: "center" as const,
      render: (item) => {
        if (item.table === "mt5_bars") return <Badge variant="ghost">阻塞</Badge>;
        return item.stale
          ? <Badge variant="danger">⚠ 过期</Badge>
          : <Badge variant="success">✓ 正常</Badge>;
      },
    },
    {
      key: "note",
      header: "说明",
      render: (item) => <span className="text-fg-muted text-xs">{item.note || ""}</span>,
    },
  ];

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">外部数据时效</h1>
        <div className="flex items-center gap-2">
          {msg && (
            <span className={`text-xs ${msg.includes("✅") ? "text-up" : "text-down"}`}>
              {msg}
            </span>
          )}
          <Button onClick={handleRefresh} disabled={refreshing} loading={refreshing}>
            {refreshing ? "刷新中..." : "🔄 一键刷新"}
          </Button>
          <Button variant="secondary" size="sm" onClick={refresh}>
            刷新状态
          </Button>
        </div>
      </div>

      <Table
        columns={extColumns}
        data={sources}
        keyExtractor={(item) => item.table}
        loading={loading}
        emptyMessage="暂无外部数据状态"
      />

      <div className="text-fg-muted text-xs space-y-1">
        <p>• COT (CFTC 持仓) — 周度更新，每次约 30-60s</p>
        <p>• Events (经济日历) — 日度更新，每次约 3-10s</p>
        <p>• ETF (GLD/SLV 持仓) — 季度更新，每次约 30s</p>
        <p>• MT5 bars — ⏸ 阻塞 (MetaTrader5 IPC pipe 不兼容)</p>
        <p>• 也可 <code className="bg-gray-200 px-1 rounded">python start-all.py --refresh-data</code> 启动时自动刷新</p>
      </div>
    </div>
  );
}

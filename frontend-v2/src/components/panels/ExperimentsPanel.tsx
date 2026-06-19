import { useEffect, useState } from "react";
import { Button, Card, ProgressBar, Input, Select, Table, TabBar } from "@/components/ui";
import type { Column } from "@/components/ui";
import { useJobPolling, useApi } from "@/lib/hooks";
import { authFetch } from "@/lib/auth";

export default function ExperimentsPanel() {
  const [activeTab, setActiveTab] = useState("tuning");

  return (
    <div>
      <TabBar
        tabs={[
          { key: "tuning", label: "调参" },
          { key: "calibrator", label: "校准" },
          { key: "ab", label: "A/B测试" },
        ]}
        active={activeTab}
        onChange={setActiveTab}
      />

      {activeTab === "tuning" && <TuningSection />}
      {activeTab === "calibrator" && <CalibratorSection />}
      {activeTab === "ab" && <ABSection />}
    </div>
  );
}

/* ===== 调参 (from Tuning.tsx) ===== */
function TuningSection() {
  const [riskGrid, setRiskGrid] = useState("0.5,1.0,1.5,2.0");
  const [cbGrid, setCbGrid] = useState("5,10,15,20");
  const [nBars, setNBars] = useState(5000);
  const [jobId, setJobId] = useState<string | null>(null);
  const [report, setReport] = useState<string | null>(null);

  const poller = useJobPolling((id: string) => `/api/tuning/${id}`);
  const isRunning =
    poller.progress?.status === "running" ||
    poller.progress?.status === "queued";

  // When the poll signals done, fetch the full report file from the backend.
  useEffect(() => {
    if (!poller.done || poller.error) return;

    const reportPath: string | undefined = poller.result?.report_path;
    if (!reportPath) {
      setReport(JSON.stringify(poller.result ?? {}, null, 2));
      return;
    }

    let cancelled = false;
    const name = reportPath.split(/[\\\\/]/).pop()!;

    authFetch(`/api/reports/${encodeURIComponent(name)}`)
      .then(async (rr) => {
        if (cancelled) return;
        if (!rr.ok) {
          setReport(
            `报告读取失败 (${rr.status}); result: ${JSON.stringify(poller.result, null, 2)}`
          );
          return;
        }
        const dd = await rr.json();
        if (cancelled) return;

        const text =
          typeof dd.content === "string"
            ? dd.content
            : JSON.stringify(dd.content, null, 2);

        const summary = [
          "# 调参完成",
          `# 最优: ${JSON.stringify(poller.result?.best)}`,
          `# top N: ${poller.result?.top?.length ?? 0} 组`,
          `# report: ${reportPath}`,
          "",
        ].join("\n");

        setReport(summary + text);
      });

    return () => {
      cancelled = true;
    };
  }, [poller.done, poller.result, poller.error]);

  async function start() {
    setReport(null);

    const r = await authFetch("/api/tuning/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        risk_pct_grid: riskGrid
          .split(",")
          .map((s) => parseFloat(s.trim()))
          .filter((n) => !isNaN(n)),
        cb_pct_grid: cbGrid
          .split(",")
          .map((s) => parseFloat(s.trim()))
          .filter((n) => !isNaN(n)),
        n_bars: nBars,
      }),
    });
    if (!r.ok) return;
    const d = await r.json();
    setJobId(d.job_id);
    poller.start(d.job_id);
  }

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">风险参数调参</h1>

      <Card>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          <Input
            label="risk_pct_grid (csv)"
            type="text"
            value={riskGrid}
            onChange={(e) => setRiskGrid(e.target.value)}
          />
          <Input
            label="cb_pct_grid (csv)"
            type="text"
            value={cbGrid}
            onChange={(e) => setCbGrid(e.target.value)}
          />
          <Input
            label="n_bars"
            type="number"
            value={nBars}
            onChange={(e) => setNBars(parseInt(e.target.value) || 0)}
          />
        </div>
      </Card>

      <div className="flex gap-2 items-center">
        <Button onClick={start} disabled={isRunning} loading={isRunning}>
          开始调参
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

      {report && (
        <Card title="报告 (末 2000 字符)">
          <pre className="text-xs whitespace-pre-wrap num text-fg">
            {report}
          </pre>
        </Card>
      )}
    </div>
  );
}

/* ===== 校准 (from Calibrator.tsx) ===== */
interface CalibratorStatus {
  path: string;
  exists: boolean;
  buckets: any[] | null;
  platt?: { a: number; b: number };
  last_modified?: string;
  error?: string;
}

function CalibratorSection() {
  const { data, loading, refresh } = useApi<CalibratorStatus>(
    "/api/calibrator"
  );
  const [editing, setEditing] = useState<string>("");
  const [saved, setSaved] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [loadMsg, setLoadMsg] = useState<string | null>(null);
  const [loadingCal, setLoadingCal] = useState(false);

  useEffect(() => {
    if (data?.buckets) {
      setEditing(JSON.stringify(data.buckets, null, 2));
    } else if (data && !data.buckets) {
      setEditing("[]");
    }
  }, [data?.buckets]);

  async function loadCalibrator() {
    setLoadingCal(true);
    setLoadMsg(null);
    try {
      const r = await authFetch("/api/calibrator/load", { method: "POST" });
      if (r.ok) {
        const d = await r.json();
        setLoadMsg(`✓ 已加载: ${d.path ?? "ok"}`);
        await refresh();
      } else {
        const err = await r.json().catch(() => ({}));
        setLoadMsg(`✗ 加载失败 (${r.status}): ${err.detail?.msg ?? r.statusText}`);
      }
    } catch (e: any) {
      setLoadMsg(`✗ 请求失败: ${e.message}`);
    } finally {
      setLoadingCal(false);
    }
  }

  async function saveBuckets() {
    setSaving(true);
    setSaved(null);
    try {
      const parsed = JSON.parse(editing);
      const normalized: Array<{
        low: number;
        high: number;
        calibrated: number;
      }> = [];
      if (Array.isArray(parsed)) {
        for (const b of parsed) {
          if (Array.isArray(b) && b.length >= 3) {
            normalized.push({
              low: Number(b[0]),
              high: Number(b[1]),
              calibrated: Number(b[2]),
            });
          } else if (b && typeof b === "object") {
            normalized.push({
              low: Number(b.low ?? b.bin ?? 0),
              high: Number(b.high ?? b.bin ?? 0),
              calibrated: Number(b.calibrated ?? b.raw ?? 0),
            });
          }
        }
      }
      const r = await authFetch("/api/calibrator/save", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ buckets: normalized }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        setSaved(
          `保存失败 (${r.status}): ${err.detail?.msg ?? r.statusText}`
        );
        return;
      }
      const d = await r.json();
      setSaved(`保存 ${d.saved_n} 桶到 ${d.path}`);
      await refresh();
    } catch (e: any) {
      setSaved(`解析/保存失败: ${e.message}`);
    } finally {
      setSaving(false);
    }
  }

  // B7 fix: 后端 buckets 形状是 [[lo, hi, calibrated], ...] (3-tuple 一列 per bin, 单一 calibrated 率)
  // 前端之前期望 {low, high, raw, calibrated, n} 4-字段 dict, 实际后端没 raw 字段 (calibrator 是单调映射 raw → calibrated)
  // 修法: 列重新设计成 [bin, calibrated, source_flag]
  const bucketColumns: Column<any>[] = [
    {
      key: "bin",
      header: "bin",
      align: "right",
      render: (b: any) => {
        const low = Array.isArray(b) ? b[0] : b.low ?? b.bin_lo;
        const high = Array.isArray(b) ? b[1] : b.high ?? b.bin_hi;
        if (Number.isFinite(low) && Number.isFinite(high)) {
          return `${low.toFixed(2)}–${high.toFixed(2)}`;
        }
        return "--";
      },
    },
    {
      key: "calibrated",
      header: "calibrated",
      align: "right",
      render: (b: any) => {
        const cal = Array.isArray(b) ? b[2] : b.calibrated;
        return Number.isFinite(cal) ? cal.toFixed(3) : "--";
      },
    },
    {
      key: "source",
      header: "source",
      align: "right",
      render: (b: any) => {
        // 3-tuple = bucket data, 4-dict = legacy. 标个 source 帮 debug.
        return Array.isArray(b) ? "bucket" : "legacy";
      },
    },
  ];

  return (
    <div className="space-y-4">
      <h1
        className="text-2xl font-bold"
        style={{ color: "#e6edf3" }}
      >
        概率校准器
      </h1>

      <Card title="校准信息" className="space-y-2">
        <div style={{ color: "#8b949e" }}>路径</div>
        <div
          className="font-mono text-xs"
          style={{ color: "#8b949e" }}
        >
          {data?.path ?? "--"}
        </div>
        {data && (
          <div className="flex gap-4 text-xs flex-wrap">
            <span>
              存在:{" "}
              <span
                style={{
                  color: data.exists ? "#3fb950" : "#f85149",
                }}
              >
                {data.exists ? "是" : "否"}
              </span>
            </span>
            {data.last_modified && (
              <span>
                修改:{" "}
                <span
                  className="num"
                  style={{ color: "#8b949e" }}
                >
                  {data.last_modified}
                </span>
              </span>
            )}
            {data.platt?.a != null && data.platt?.b != null && (
              <span>
                Platt:{" "}
                <span className="num">
                  a=
                  {Number.isFinite(data.platt.a)
                    ? data.platt.a.toFixed(3)
                    : "--"}{" "}
                  b=
                  {Number.isFinite(data.platt.b)
                    ? data.platt.b.toFixed(3)
                    : "--"}
                </span>
              </span>
            )}
          </div>
        )}
      </Card>

      {data?.buckets && (
        <Table
          columns={bucketColumns}
          data={data.buckets}
          keyExtractor={(_b, i) => String(i)}
          loading={loading}
          emptyMessage="无桶数据"
        />
      )}

      <Card title="编辑 buckets JSON" subtitle="高级">
        <div className="space-y-2">
          <textarea
            value={editing}
            onChange={(e) => setEditing(e.target.value)}
            rows={10}
            className="w-full bg-white border border-[#dce0e6] rounded p-2 font-mono text-xs num text-[#1a1e24]"
            placeholder="[]"
          />
          <div className="flex gap-2 items-center">
            <Button
              variant="primary"
              onClick={saveBuckets}
              loading={saving}
            >
              保存
            </Button>
            <Button
              variant="secondary"
              onClick={refresh}
              disabled={loading}
            >
              重载
            </Button>
            <Button
              variant="secondary"
              onClick={loadCalibrator}
              disabled={loadingCal}
              loading={loadingCal}
            >
              加载校准
            </Button>
            {saved && (
              <span
                className="text-xs"
                style={{ color: "#8b949e" }}
              >
                {saved}
              </span>
            )}
            {loadMsg && (
              <span
                className={`text-xs ${loadMsg.startsWith("✓") ? "text-up" : "text-down"}`}
              >
                {loadMsg}
              </span>
            )}
          </div>
        </div>
      </Card>
    </div>
  );
}

/* ===== A/B测试 (from AB.tsx) ===== */
const PATH_OPTIONS = [
  { value: "baseline", label: "baseline" },
  { value: "uniform", label: "uniform" },
  { value: "reverse", label: "reverse" },
];

function ABSection() {
  const [pathA, setPathA] = useState("baseline");
  const [pathB, setPathB] = useState("reverse");
  const [nBars, setNBars] = useState(5000);
  const [jobId, setJobId] = useState<string | null>(null);
  const [report, setReport] = useState<string | null>(null);
  const [abHistory, setAbHistory] = useState<any[] | null>(null);

  const poller = useJobPolling((id: string) => `/api/ab/${id}`);
  const isRunning =
    poller.progress?.status === "running" ||
    poller.progress?.status === "queued";

  useEffect(() => {
    authFetch("/api/ab").then(async (r) => {
      if (r.ok) {
        const d = await r.json();
        setAbHistory(Array.isArray(d) ? d : d.results ?? d.history ?? d.runs ?? []);
      }
    }).catch(() => {});
  }, []);

  // When the poll signals done, fetch the full report file from the backend.
  useEffect(() => {
    if (!poller.done || poller.error) return;

    const reportPath: string | undefined = poller.result?.report_path;
    if (!reportPath) {
      setReport(JSON.stringify(poller.result ?? {}, null, 2));
      return;
    }

    let cancelled = false;
    const name = reportPath.split(/[\\\\/]/).pop()!;

    authFetch(`/api/reports/${encodeURIComponent(name)}`)
      .then(async (rr) => {
        if (cancelled) return;
        if (!rr.ok) {
          setReport(
            `报告读取失败 (${rr.status}); result: ${JSON.stringify(poller.result, null, 2)}`
          );
          return;
        }
        const dd = await rr.json();
        if (cancelled) return;

        const text =
          typeof dd.content === "string"
            ? dd.content
            : JSON.stringify(dd.content, null, 2);

        const summary = [
          "# A/B 完成",
          `# delta_pnl: ${poller.result?.delta_pnl}`,
          `# delta_sharpe: ${poller.result?.delta_sharpe}`,
          `# report: ${reportPath}`,
          "",
        ].join("\n");

        setReport(summary + text);
      });

    return () => {
      cancelled = true;
    };
  }, [poller.done, poller.result, poller.error]);

  async function start() {
    setReport(null);

    const r = await authFetch("/api/ab/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path_a: pathA, path_b: pathB, n_bars: nBars }),
    });
    if (!r.ok) return;
    const d = await r.json();
    setJobId(d.job_id);
    poller.start(d.job_id);
  }

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">A/B 测试</h1>

      <Card>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          <Select
            label="path A"
            options={PATH_OPTIONS}
            value={pathA}
            onChange={(e) => setPathA(e.target.value)}
          />
          <Select
            label="path B"
            options={PATH_OPTIONS}
            value={pathB}
            onChange={(e) => setPathB(e.target.value)}
          />
          <Input
            label="n_bars"
            type="number"
            value={nBars}
            onChange={(e) => setNBars(parseInt(e.target.value) || 0)}
          />
        </div>
      </Card>

      <div className="flex gap-2 items-center">
        <Button onClick={start} disabled={isRunning} loading={isRunning}>
          开始 A/B
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

      {report && (
        <Card title="报告 (末 2000 字符)">
          <pre className="text-xs whitespace-pre-wrap num text-fg">
            {report}
          </pre>
        </Card>
      )}

      {abHistory && abHistory.length > 0 && (
        <Card title="历史 A/B 测试">
          <Table
            columns={[
              { key: "id", header: "ID", render: (r: any) => <span className="text-xs font-mono">{r.id ?? r.job_id ?? "--"}</span> },
              { key: "path_a", header: "path A", render: (r: any) => r.path_a ?? r.a ?? "--" },
              { key: "path_b", header: "path B", render: (r: any) => r.path_b ?? r.b ?? "--" },
              { key: "delta_pnl", header: "ΔPnL", align: "right", render: (r: any) => <span className="num">{(r.delta_pnl ?? 0).toFixed(2)}</span> },
              { key: "delta_sharpe", header: "ΔSharpe", align: "right", render: (r: any) => <span className="num">{(r.delta_sharpe ?? 0).toFixed(4)}</span> },
              { key: "ts", header: "时间", render: (r: any) => <span className="text-xs text-fg-muted">{r.ts ?? r.timestamp ?? r.created_at ?? "--"}</span> },
            ]}
            data={abHistory}
            keyExtractor={(r: any, i: number) => r.id ?? r.job_id ?? String(i)}
            emptyMessage="暂无历史"
          />
        </Card>
      )}
    </div>
  );
}

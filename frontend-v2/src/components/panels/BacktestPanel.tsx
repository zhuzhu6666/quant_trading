import { useState, useEffect, useRef } from "react";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Table } from "@/components/ui/Table";
import type { Column } from "@/components/ui";
import { KpiCard } from "@/components/dashboard/KpiCard";
import { MiniAreaChart } from "@/components/dashboard/MiniAreaChart";
import { authFetch } from "@/lib/auth";

const TABS = ["回测", "历史", "对比"];

export default function BacktestPanel() {
  const [tab, setTab] = useState("回测");
  const [symbol, setSymbol] = useState("XAUUSD+");
  const [timeframe, setTimeframe] = useState("M15");
  const [riskPct, setRiskPct] = useState("1.0");
  const [useCircuit, setUseCircuit] = useState(true);
  const [jobId, setJobId] = useState<string | null>(null);
  const [jobStatus, setJobStatus] = useState<any>(null);
  const [report, setReport] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [backtestHistory, setBacktestHistory] = useState<any[]>([]);
  const [latestReport, setLatestReport] = useState<any>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    authFetch("/api/backtest").then(async (r) => {
      if (!r.ok) return;
      const d = await r.json();
      setBacktestHistory(Array.isArray(d) ? d : d.results ?? d.backtests ?? []);
    }).catch(() => {});
    authFetch("/api/backtest/report/latest").then(async (r) => {
      if (!r.ok) return;
      setLatestReport(await r.json());
    }).catch(() => {});
  }, []);

  const startBacktest = async () => {
    setLoading(true);
    setJobId(null);
    setReport(null);
    try {
      const r = await authFetch("/api/backtest/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbol,
          timeframe,
          risk_per_trade_pct: parseFloat(riskPct),
          enable_circuit: useCircuit,
        }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      setJobId(d.job_id);
      setJobStatus({ status: "pending", job_id: d.job_id });
      startPolling(d.job_id);
    } catch (e: any) {
      setJobStatus({ error: e?.message ?? String(e) });
    } finally {
      setLoading(false);
    }
  };

  const fetchReport = async (id: string) => {
    try {
      const r = await authFetch(`/api/backtest/${id}`);
      if (!r.ok) return;
      const st = await r.json();
      setJobStatus(st);
      if (st.status === "done") {
        const rr = await authFetch(`/api/backtest/${id}/report`);
        if (rr.ok) {
          const rep = await rr.json();
          setReport(rep.report);
        }
        // Auto-stop polling when job completes
        if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
      } else if (st.status === "error" || st.status === "cancelled") {
        if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
      }
    } catch { /* ignore */ }
  };

  const startPolling = (id: string) => {
    // Clear any existing poll interval before starting a new one
    if (pollRef.current) clearInterval(pollRef.current);
    fetchReport(id); // immediate first fetch
    pollRef.current = setInterval(() => fetchReport(id), 3000);
  };

  // Cleanup polling on unmount
  useEffect(() => {
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, []);

  return (
    <div className="space-y-4">
      {/* Tab Bar */}
      <div className="flex items-center gap-1 bg-apple-bg rounded-xl p-1">
        {TABS.map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`flex-1 py-2 text-xs font-medium rounded-lg transition-all duration-200 ${
              tab === t
                ? "bg-white text-text-primary shadow-apple-sm"
                : "text-text-secondary hover:text-text-primary"
            }`}
          >
            {t}
          </button>
        ))}
      </div>

      {/* 回测 */}
      {tab === "回测" && (
        <div className="space-y-4">
          {/* Config */}
          <Card title="参数配置" padding="md">
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <div>
                <label className="section-label mb-2 block">品种</label>
                <select
                  value={symbol}
                  onChange={(e) => setSymbol(e.target.value)}
                  className="w-full px-3 py-2 rounded-xl bg-apple-bg text-sm text-text-primary border border-apple-border focus:border-accent focus:outline-none transition-colors"
                >
                  <option value="XAUUSD+">XAUUSD+</option>
                </select>
              </div>
              <div>
                <label className="section-label mb-2 block">周期</label>
                <select
                  value={timeframe}
                  onChange={(e) => setTimeframe(e.target.value)}
                  className="w-full px-3 py-2 rounded-xl bg-apple-bg text-sm text-text-primary border border-apple-border focus:border-accent focus:outline-none transition-colors"
                >
                  <option value="M5">M5</option>
                  <option value="M15">M15</option>
                  <option value="H1">H1</option>
                </select>
              </div>
              <div>
                <label className="section-label mb-2 block">风险%/trade</label>
                <select
                  value={riskPct}
                  onChange={(e) => setRiskPct(e.target.value)}
                  className="w-full px-3 py-2 rounded-xl bg-apple-bg text-sm text-text-primary border border-apple-border focus:border-accent focus:outline-none transition-colors"
                >
                  <option value="0.5">0.5%</option>
                  <option value="1.0">1.0%</option>
                  <option value="1.5">1.5%</option>
                  <option value="2.0">2.0%</option>
                </select>
              </div>
              <div>
                <label className="section-label mb-2 block">熔断</label>
                <button
                  onClick={() => setUseCircuit(!useCircuit)}
                  className={`w-full px-3 py-2 rounded-xl text-sm font-medium transition-all duration-200 ${
                    useCircuit
                      ? "bg-success text-white"
                      : "bg-apple-bg text-text-secondary border border-apple-border"
                  }`}
                >
                  {useCircuit ? "已启用" : "已禁用"}
                </button>
              </div>
            </div>
            <div className="mt-4 flex items-center gap-3">
              <Button variant="primary" size="md" onClick={startBacktest} disabled={loading}>
                {loading ? "提交中..." : "启动回测"}
              </Button>
              <span className="text-2xs text-text-secondary">
                数据长度: 202,865 bars (默认全量)
              </span>
            </div>
          </Card>

          {/* Job Status */}
          {jobId && (
            <Card title="回测任务" padding="md">
              <div className="space-y-2 text-sm">
                <div className="flex items-center justify-between">
                  <span className="text-text-secondary">Job ID</span>
                  <span className="font-medium">{jobId}</span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-text-secondary">状态</span>
                  <Badge variant={jobStatus?.status === "done" ? "success" : jobStatus?.status === "error" ? "danger" : "warning"}>
                    {jobStatus?.status ?? "pending"}
                  </Badge>
                </div>
                {jobStatus?.status === "done" && (
                  <div className="flex items-center justify-between">
                    <span className="text-text-secondary">报告</span>
                    <Button variant="ghost" size="sm" onClick={() => fetchReport(jobId)}>
                      查看报告
                    </Button>
                  </div>
                )}
              </div>
            </Card>
          )}

          {/* Report */}
          {report && (
            <Card title="回测报告" padding="md">
              <div className="space-y-2 text-sm">
                <div className="text-text-secondary">报告已加载</div>
                <pre className="text-xs bg-apple-bg rounded-xl p-3 overflow-auto max-h-[300px]">
                  {JSON.stringify(report, null, 2)}
                </pre>
              </div>
            </Card>
          )}
        </div>
      )}

      {/* 历史 */}
      {tab === "历史" && (
        <div className="space-y-4">
          {latestReport && (
            <Card title="最新回测报告" padding="md">
              <pre className="text-xs bg-apple-bg rounded-xl p-3 overflow-auto max-h-[200px]">
                {JSON.stringify(latestReport, null, 2)}
              </pre>
            </Card>
          )}
          <Card title="历史回测列表" padding="md">
            <Table
              columns={[
                { key: "id", header: "ID", render: (r: any) => <span className="text-xs font-mono">{r.id ?? r.job_id ?? "--"}</span> },
                { key: "symbol", header: "品种", render: (r: any) => r.symbol ?? "--" },
                { key: "timeframe", header: "周期", render: (r: any) => r.timeframe ?? "--" },
                { key: "status", header: "状态", render: (r: any) => <Badge variant={r.status === "done" ? "success" : r.status === "error" ? "danger" : "warning"}>{r.status ?? "--"}</Badge> },
                { key: "pnl", header: "PnL", align: "right", render: (r: any) => <span className="num">{(r.pnl ?? 0).toFixed(2)}</span> },
                { key: "sharpe", header: "Sharpe", align: "right", render: (r: any) => <span className="num">{(r.sharpe ?? 0).toFixed(3)}</span> },
                { key: "ts", header: "时间", render: (r: any) => <span className="text-xs text-fg-muted">{r.ts ?? r.timestamp ?? r.created_at ?? "--"}</span> },
              ]}
              data={backtestHistory}
              keyExtractor={(r: any, i: number) => r.id ?? r.job_id ?? String(i)}
              emptyMessage="暂无回测历史"
            />
          </Card>
        </div>
      )}

      {/* 对比 */}
      {tab === "对比" && (
        <Card title="回测对比" padding="md">
          <div className="text-sm text-text-secondary text-center py-8">
            选择 2 个历史回测结果进行对比
          </div>
        </Card>
      )}
    </div>
  );
}

"use client";
import { useState } from "react";
import { authFetch } from "@/lib/auth";
import { useAppStore } from "@/lib/store";
import { fmtNum, fmtPct, fmtUSD, classNames } from "@/lib/format";

export default function Overview() {
  const snapshot = useAppStore((s) => s.snapshot);
  const [jobId, setJobId] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  async function runBacktest() {
    setRunning(true);
    try {
      const r = await authFetch("/api/backtest/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: "XAUUSD+", timeframe: "M15" }),
      });
      const data = await r.json();
      setJobId(data.job_id);
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">总览</h1>
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        <Card title="账户权益">
          <div className="num text-3xl font-bold">{snapshot ? fmtNum(snapshot.equity) : "--"}</div>
          <div className="text-fg-muted text-sm">余额 {snapshot ? fmtNum(snapshot.balance) : "--"}</div>
        </Card>
        <Card title="今日 PnL">
          <div className={classNames("num text-3xl font-bold", (snapshot?.pnl_today ?? 0) >= 0 ? "text-up" : "text-down")}>
            {snapshot ? fmtUSD(snapshot.pnl_today) : "--"}
          </div>
          <div className="text-fg-muted text-sm">
            交易 {snapshot?.daily.trades ?? 0} 胜 {snapshot?.daily.win ?? 0} 负 {snapshot?.daily.loss ?? 0}
          </div>
        </Card>
        <Card title="当前持仓">
          <div className={classNames("text-3xl font-bold",
            snapshot?.position?.dir === "LONG" ? "text-up" :
            snapshot?.position?.dir === "SHORT" ? "text-down" : "text-fg-muted"
          )}>
            {snapshot?.position?.dir ?? "FLAT"}
          </div>
          {snapshot?.position?.dir !== "FLAT" && snapshot && (
            <div className="text-fg-muted text-sm num">
              @ {fmtNum(snapshot.position.entry)} 浮动 {fmtUSD(snapshot.position.unrealized)}
            </div>
          )}
        </Card>
        <Card title="风控">
          <div className="num text-2xl">
            DD <span className="text-warn">{fmtPct(snapshot?.daily.drawdown_pct ?? 0)}</span>
          </div>
          <div className="text-fg-muted text-sm">
            连续亏损 {snapshot?.risk.consecutive_loss ?? 0}  熔断 {snapshot?.risk.circuit_breaker ? "触发" : "正常"}
          </div>
        </Card>
        <Card title="回测">
          <button
            onClick={runBacktest}
            disabled={running}
            className="bg-accent text-bg font-semibold px-4 py-2 rounded disabled:opacity-50"
          >
            {running ? "提交中..." : "▶ 跑一次回测"}
          </button>
          {jobId && <div className="text-fg-muted text-sm mt-2">job: {jobId}</div>}
        </Card>
        <Card title="时间">
          <div className="num text-fg-muted text-sm">{snapshot?.server_time ?? "--"}</div>
        </Card>
      </div>
    </div>
  );
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-bg-card border border-bg-border rounded-lg p-4">
      <div className="text-fg-muted text-sm mb-2">{title}</div>
      {children}
    </div>
  );
}

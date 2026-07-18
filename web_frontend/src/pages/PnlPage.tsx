import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { BarChart3, CalendarClock, ListChecks, Percent, TrendingDown, TrendingUp } from "lucide-react";
import { getRealizedPnlSeries } from "@/api/client";
import { MetricCard } from "@/components/Card";
import { Field, numberTone, StatTile } from "@/components/DashboardBits";
import { StatusPill } from "@/components/StatusPill";
import { formatDecimal, formatMoney, formatTime } from "@/lib/format";
import { asRecord, formatDirection, pick, pickArray, pickNumber, pickString } from "@/lib/compat";
import { translateDisplayValue, translateScope } from "@/lib/display";
import { factBoundTone, factHasDisplayValue, factIsKnown, readFact } from "@/api/fact";

const scopes = ["today", "24h", "7d", "30d", "all"] as const;

type RealizedPoint = {
  ts: number;
  cumulative: number;
  pnl: number;
  symbol?: string;
  position_id?: number;
  deal_id?: number;
  source?: string;
  direction?: number | string;
};

function buildPath(points: RealizedPoint[], width = 720, height = 220, pad = 18): string {
  if (!points.length) return "";
  const values = points.map((point) => point.cumulative || 0);
  const minValue = Math.min(...values, 0);
  const maxValue = Math.max(...values, 0);
  const xAt = (index: number) => pad + (index / Math.max(points.length - 1, 1)) * (width - pad * 2);
  const yAt = (value: number) => {
    if (maxValue === minValue) return height / 2;
    return pad + ((maxValue - value) / (maxValue - minValue)) * (height - pad * 2);
  };
  return points
    .map((point, idx) => `${idx === 0 ? "M" : "L"}${xAt(idx).toFixed(2)},${yAt(point.cumulative).toFixed(2)}`)
    .join(" ");
}

function safeCurrency(raw: unknown): string {
  return String(raw || "");
}

export function PnlPage() {
  const [scope, setScope] = useState<(typeof scopes)[number]>("today");

  const seriesQuery = useQuery({
    queryKey: ["realized-pnl", scope],
    queryFn: () => getRealizedPnlSeries(scope),
    refetchInterval: 10_000,
    staleTime: 5_000,
  });
  const seriesFact = readFact(seriesQuery.data, "live.realized-pnl.v2");
  const seriesRequestFailed = seriesQuery.isError || seriesQuery.isRefetchError;
  const seriesKnown = factIsKnown(seriesFact, seriesRequestFailed);
  const seriesDisplayable = factHasDisplayValue(seriesFact);

  const points = useMemo(
    () => seriesDisplayable
      ? pickArray(seriesQuery.data, ["points", "data", "items", "payload.points"]) as RealizedPoint[]
      : [],
    [seriesDisplayable, seriesQuery.data],
  );
  const summary = seriesDisplayable ? asRecord(pick(seriesQuery.data, ["summary"])) : {};
  const realized = pickNumber(summary, ["realized_pnl", "realizedPnl", "pnl"], 0);
  const trades = pickNumber(summary, ["trades", "count"], points.length);
  const wins = pickNumber(summary, ["wins", "win"], 0);
  const losses = pickNumber(summary, ["losses", "loss"], 0);
  const rawWinRate = pickNumber(summary, ["win_rate", "winRate", "rate"], trades ? wins / trades : 0);
  const winRate = rawWinRate > 1 ? rawWinRate : rawWinRate * 100;
  const currency = safeCurrency(pickString(summary, ["currency", "ccy", "symbol"], pickString(seriesQuery.data, ["currency"], "")));
  const fromTs = pick(seriesQuery.data, ["from_ts", "fromTs", "start", "start_ts"]);
  const toTs = pick(seriesQuery.data, ["to_ts", "toTs", "end", "end_ts"]);

  const pathD = useMemo(() => buildPath(points), [points]);
  const hasData = points.length > 0;
  const firstTs = points[0]?.ts || 0;
  const lastTs = points[points.length - 1]?.ts || 0;
  const lastPoint = points[points.length - 1];
  const bestTrade = points.reduce((best, point) => Math.max(best, Number(point.pnl || 0)), 0);
  const worstTrade = points.reduce((worst, point) => Math.min(worst, Number(point.pnl || 0)), 0);
  const avgTrade = trades ? realized / trades : 0;
  const hasSummaryData = Object.keys(summary).length > 0;

  return (
    <section className="dashboard">
      <div className="dashboard-header">
        <div>
          <div className="eyebrow">收益表现</div>
          <h1>已实现盈亏</h1>
          <p>按时间窗口查看真实成交收益、胜率和最近成交明细。</p>
        </div>
        <div className="header-status">
          <StatusPill status={`窗口 ${translateScope(scope)}`} tone="mute" />
          <StatusPill
            status={seriesRequestFailed ? "刷新失败，保留上次事实" : seriesQuery.isFetching ? "刷新中" : seriesKnown ? "已同步" : seriesFact.state === "stale" ? "事实已过期" : "事实未知"}
            tone={factBoundTone(seriesFact, seriesKnown ? "ok" : "warn", seriesRequestFailed)}
          />
        </div>
      </div>

      <div className="scope-picker scope-picker-large" role="group" aria-label="收益时间窗口">
        {scopes.map((item) => (
          <button
            key={item}
            className={`scope-btn ${scope === item ? "active" : ""}`}
            type="button"
            aria-pressed={scope === item}
            onClick={() => setScope(item)}
          >
            {translateScope(item)}
          </button>
        ))}
      </div>

      <div className="stat-grid">
        {hasSummaryData || hasData ? <StatTile icon={realized >= 0 ? TrendingUp : TrendingDown} label="已实现盈亏" value={formatMoney(realized, currency)} detail={lastPoint ? `最新 ${formatMoney(lastPoint.pnl, currency)}` : undefined} tone={factBoundTone(seriesFact, numberTone(realized), seriesRequestFailed)} /> : null}
        {hasSummaryData || hasData ? <StatTile icon={ListChecks} label="交易数" value={formatDecimal(trades, 0)} detail={trades ? `胜 ${formatDecimal(wins, 0)} / 负 ${formatDecimal(losses, 0)}` : undefined} tone={factBoundTone(seriesFact, trades ? "ok" : "mute", seriesRequestFailed)} /> : null}
        {trades > 0 ? <StatTile icon={Percent} label="胜率" value={`${formatDecimal(winRate, 1)}%`} detail={`均值 ${formatMoney(avgTrade, currency)}`} tone={factBoundTone(seriesFact, winRate >= 50 ? "ok" : "warn", seriesRequestFailed)} /> : null}
        <StatTile icon={CalendarClock} label="数据窗口" value={fromTs ? formatTime(fromTs) : ""} detail={toTs ? `至 ${formatTime(toTs)}` : "等待数据"} tone={factBoundTone(seriesFact, hasData ? "ok" : "mute", seriesRequestFailed)} />
      </div>

      <div className="dashboard-grid">
        <MetricCard title="收益曲线" className="wide-panel">
          {seriesQuery.isLoading ? <p className="loading-state">加载中...</p> : null}
          {seriesRequestFailed ? (
            <p className="error-text">接口异常：{seriesQuery.error instanceof Error ? seriesQuery.error.message : "请求失败"}</p>
          ) : null}
          {!seriesDisplayable && !seriesRequestFailed ? <p className="loading-state">收益事实未知，等待权威数据。</p> : null}
          <div className="pnl-window-summary" aria-label="窗口摘要">
            <Field label="币种" value={currency} />
            <Field label="最佳单笔" value={formatMoney(bestTrade, currency)} tone={factBoundTone(seriesFact, bestTrade > 0 ? "ok" : "mute", seriesRequestFailed)} />
            <Field label="最差单笔" value={formatMoney(worstTrade, currency)} tone={worstTrade < 0 ? "bad" : "mute"} />
            <Field label="平均单笔" value={formatMoney(avgTrade, currency)} tone={factBoundTone(seriesFact, numberTone(avgTrade), seriesRequestFailed)} />
            <Field label="首条成交" value={firstTs ? formatTime(firstTs) : ""} />
            <Field label="末条成交" value={lastTs ? formatTime(lastTs) : ""} />
          </div>
          <div className="chart-card chart-card-strong">
            <div className="mini-chart">
              {hasData ? (
                <svg viewBox="0 0 720 220" preserveAspectRatio="none" role="img" aria-label={`累计已实现盈亏曲线，共 ${points.length} 个成交点，累计 ${formatMoney(realized, currency)}`}>
                  <title>{`累计已实现盈亏 ${formatMoney(realized, currency)}`}</title>
                  <rect x={0} y={0} width={720} height={220} className="chart-bg" />
                  <path d="M18 110H702" className="chart-zero" />
                  <path d={pathD} fill="none" className="chart-line" />
                </svg>
              ) : (
                <div className="chart-empty">
                  <BarChart3 size={28} />
                  <span>当前范围暂无成交数据</span>
                </div>
              )}
            </div>
            <div className="chart-meta">
              {pickString(seriesQuery.data, ["source", "provider", "origin"], "") ? <span>来源：{pickString(seriesQuery.data, ["source", "provider", "origin"], "")}</span> : null}
              <span>{firstTs ? `${formatTime(firstTs)} 到 ${formatTime(lastTs)}` : "等待成交点"}</span>
            </div>
            {hasData ? <p className="chart-summary">本窗口累计 {formatMoney(realized, currency)}，最佳单笔 {formatMoney(bestTrade, currency)}，最差单笔 {formatMoney(worstTrade, currency)}。</p> : null}
          </div>
        </MetricCard>

        <MetricCard title="最近成交明细" className="wide-panel">
          <div className="table-wrap">
            <table className="mobile-card-table pnl-deals-table">
              <thead>
                <tr>
                  <th scope="col">时间</th>
                  <th scope="col">持仓ID</th>
                  <th scope="col">成交ID</th>
                  <th scope="col">方向</th>
                  <th scope="col">品种</th>
                  <th scope="col">累计盈亏</th>
                  <th scope="col">单笔盈亏</th>
                  <th scope="col">来源</th>
                </tr>
              </thead>
              <tbody>
                {!hasData ? (
                  <tr>
                    <td colSpan={8} className="empty-state-small">当前范围暂无成交明细</td>
                  </tr>
                ) : null}
                {points.slice(-50).reverse().map((row, idx) => (
                  <tr key={`${row.deal_id || row.position_id || idx}-${row.ts}`}>
                    <td>{formatTime(row.ts)}</td>
                    <td>{row.position_id ?? ""}</td>
                    <td>{row.deal_id ?? ""}</td>
                    <td>{translateDisplayValue(formatDirection(row.direction))}</td>
                    <td>{row.symbol || ""}</td>
                    <td>{formatMoney(row.cumulative, currency)}</td>
                    <td className={Number(row.pnl || 0) < 0 ? "status-bad" : seriesKnown ? "status-ok" : "status-warn"}>{formatMoney(row.pnl, currency)}</td>
                    <td>{row.source || ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="summary-note">共 {formatDecimal(points.length, 0)} 个成交点，表格展示最近 50 条。</p>
        </MetricCard>
      </div>
    </section>
  );
}

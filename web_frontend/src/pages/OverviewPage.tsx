import {
  Activity,
  ShieldCheck,
  TrendingDown,
  TrendingUp,
  Wallet,
} from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { MetricCard } from "@/components/Card";
import { StatusPill } from "@/components/StatusPill";
import { useAuth } from "@/contexts/AuthContext";
import { useLiveState } from "@/hooks/useLiveState";
import {
  getAccount,
  getBackendReadiness,
  getHealth,
  getLoopStatus,
  getRiskSummary,
  getSessionStats,
  getSystemDbHealth,
} from "@/api/client";
import { asRecord, pick, pickArray, pickBoolean, pickNumber, pickString } from "@/lib/compat";
import { translateDisplayValue } from "@/lib/display";
import { formatDecimal, formatMoney, formatTime } from "@/lib/format";

type Tone = "ok" | "warn" | "bad" | "mute";

function toneFromStatus(status: string): Tone {
  const normalized = status.toLowerCase();
  if (["ok", "healthy", "connected", "ready", "running", "active"].includes(normalized)) return "ok";
  if (["degraded", "unknown", "idle", "warming"].includes(normalized)) return "warn";
  if (["error", "failed", "blocked", "down", "offline"].includes(normalized)) return "bad";
  return "mute";
}

function numberTone(value: number): Tone {
  if (value > 0) return "ok";
  if (value < 0) return "bad";
  return "mute";
}

function StatTile({
  label,
  value,
  detail,
  tone = "mute",
  icon: Icon,
}: {
  label: string;
  value: string;
  detail?: string;
  tone?: Tone;
  icon?: typeof Activity;
}) {
  return (
    <div className={`stat-tile stat-${tone}`}>
      <div className="stat-label">
        {Icon ? <Icon size={15} /> : null}
        <span>{label}</span>
      </div>
      <div className="stat-value">{value}</div>
      {detail ? <div className="stat-detail">{detail}</div> : null}
    </div>
  );
}

function Field({ label, value, tone }: { label: string; value: string; tone?: Tone }) {
  return (
    <div className="field-row">
      <span>{label}</span>
      {tone ? <StatusPill status={value} tone={tone} /> : <strong>{value}</strong>}
    </div>
  );
}

function hasMeaningfulText(value: unknown): boolean {
  if (value === undefined || value === null) {
    return false;
  }
  const text = String(value).trim();
  return Boolean(text && text !== "--" && text !== "null" && text !== "undefined");
}

function translatedText(value: unknown): string {
  const text = translateDisplayValue(value);
  return text === "--" ? "" : text;
}

function useDashboardQueries() {
  return {
    health: useQuery({
      queryKey: ["health"],
      queryFn: getHealth,
      refetchInterval: 10_000,
      staleTime: 5_000,
    }),
    loop: useQuery({
      queryKey: ["loop-status", "overview"],
      queryFn: getLoopStatus,
      refetchInterval: 3_000,
      staleTime: 2_000,
    }),
    account: useQuery({
      queryKey: ["account", "overview"],
      queryFn: getAccount,
      refetchInterval: 3_000,
      staleTime: 2_000,
    }),
    session: useQuery({
      queryKey: ["session-stats", "overview"],
      queryFn: getSessionStats,
      refetchInterval: 3_000,
      staleTime: 2_000,
    }),
    risk: useQuery({
      queryKey: ["risk-summary", "overview"],
      queryFn: getRiskSummary,
      refetchInterval: 10_000,
      staleTime: 5_000,
    }),
    db: useQuery({
      queryKey: ["db-health", "overview"],
      queryFn: getSystemDbHealth,
      refetchInterval: 15_000,
      staleTime: 5_000,
    }),
    readiness: useQuery({
      queryKey: ["backend-readiness", "overview"],
      queryFn: getBackendReadiness,
      refetchInterval: 15_000,
      staleTime: 5_000,
    }),
  };
}

export function OverviewPage() {
  const { authenticated } = useAuth();
  const { snapshot, source, connected, error: wsError } = useLiveState({ enabled: authenticated });
  const queries = useDashboardQueries();

  const snapshotRecord = asRecord(snapshot);
  const loop = asRecord(queries.loop.data);
  const account = { ...asRecord(queries.account.data), ...snapshotRecord };
  const session = { ...asRecord(queries.session.data), ...asRecord(snapshotRecord.daily) };
  const risk = { ...asRecord(queries.risk.data), ...asRecord(snapshotRecord.risk) };
  const db = asRecord(queries.db.data);
  const readiness = asRecord(queries.readiness.data);

  const currency = pickString(account, ["currency", "ccy"], "EUR");
  const broker = pickString(account, ["broker"], pickString(snapshotRecord, ["broker"], pickString(loop, ["broker"], "ctrader")));
  const healthStatus = pickString(queries.health.data, ["status"], "unknown");
  const dbStatus = pickString(db, ["overall", "status"], pickString(queries.health.data, ["db"], "unknown"));
  const loopRunning = pickBoolean(loop, ["running"], false);
  const strategy = pickString(loop, ["strategy", "strategy_name"], pickString(snapshotRecord.active_strategy, ["id"], "live"));
  const loopReason = translatedText(pick(loop, ["reason", "stop_reason"]));
  const executionMode = translatedText(pick(loop, ["execution_mode", "mode"]));
  const loopStartedAt = pick(loop, ["started_at", "start_time", "startedAt"]);
  const serverTime = pickString(queries.health.data, ["server_time"], pickString(snapshotRecord, ["server_time"], ""));

  const balance = pickNumber(account, ["balance"], 0);
  const equity = pickNumber(account, ["equity"], 0);
  const margin = pickNumber(account, ["margin"], 0);
  const marginFree = pickNumber(account, ["margin_free", "free_margin"], 0);
  const leverage = pickString(account, ["leverage"], "--");
  const hasMarginData = margin > 0 || marginFree > 0;
  const hasLeverageData = hasMeaningfulText(leverage) && leverage !== "0";

  const pnl = pickNumber(session, ["pnl_today", "pnl", "session_pnl"], pickNumber(snapshotRecord, ["pnl_today"], 0));
  const trades = pickNumber(session, ["trades", "session_trades"], 0);
  const wins = pickNumber(session, ["wins", "win", "session_winning"], 0);
  const losses = pickNumber(session, ["losses", "loss", "session_losing"], 0);
  const drawdown = pickNumber(session, ["drawdown_pct", "session_max_drawdown_pct"], 0);
  const winRate = trades > 0 ? (wins / trades) * 100 : 0;

  const positions = pickArray(snapshotRecord, ["positions_list"]);
  const positionCount = pickNumber(snapshotRecord, ["n_positions"], positions.length);
  const currentPrice = pickNumber(snapshotRecord, ["current_price", "spot_quote.mid", "price", "last_price"], 0);
  const priceBid = pickNumber(snapshotRecord, ["spot_quote.bid", "bid"], 0);
  const priceAsk = pickNumber(snapshotRecord, ["spot_quote.ask", "ask"], 0);
  const priceSource = pickString(snapshotRecord, ["spot_quote.source"], "");
  const priceStatus = currentPrice > 0 ? "实时" : "暂无";
  const hasSpread = priceBid > 0 && priceAsk > 0;

  const circuitBreaker = pickBoolean(risk, ["circuit_breaker"], false);
  const consecutiveLoss = pickNumber(risk, ["consecutive_loss"], losses);
  const varSummary = asRecord(pick(risk, ["var"]));
  const kellySummary = asRecord(pick(risk, ["kelly"]));
  const varStatus = pickString(varSummary, ["status"], "");
  const kellyStatus = pickString(kellySummary, ["status"], "");
  const normalizedVarStatus = varStatus.trim().toLowerCase();
  const normalizedKellyStatus = kellyStatus.trim().toLowerCase();
  const varValue = pickNumber(varSummary, ["var_pct", "var", "value"], 0);
  const varConfidence = pickNumber(varSummary, ["confidence"], 0);
  const hasVarData = Boolean(normalizedVarStatus && normalizedVarStatus !== "no data") || pickNumber(varSummary, ["current_equity"], 0) > 0;
  const hasKellyData = Boolean(normalizedKellyStatus && normalizedKellyStatus !== "no data");
  const kelly = pickNumber(kellySummary, ["kelly_fraction", "value"], 0);
  const readinessOk = pickBoolean(readiness, ["ready_for_frontend", "ready", "ok"], false);
  const blockers = pickArray(readiness, ["blockers"]);
  const dbList = pickArray(db, ["databases", "items", "rows"]);
  const dbProblems = dbList.filter((item) => {
    const freshness = pickString(item, ["freshness", "status", "state"], "");
    const exists = pickBoolean(item, ["exists"], true);
    return !exists || ["missing", "stale", "old", "error"].includes(freshness) || pickArray(item, ["errors", "issues"]).length > 0;
  }).length;
  const showDataHealth = dbProblems > 0 || blockers.length > 0 || !readinessOk || toneFromStatus(dbStatus) !== "ok";

  const apiErrors = [
    ["WS", wsError],
    ["健康接口", queries.health.error],
    ["交易循环", queries.loop.error],
    ["账户接口", queries.account.error],
    ["会话统计", queries.session.error],
    ["风控接口", queries.risk.error],
    ["数据库接口", queries.db.error],
    ["就绪接口", queries.readiness.error],
  ].filter(([, err]) => Boolean(err));

  return (
    <section className="dashboard overview-dashboard">
      <div className="dashboard-header">
        <div>
          <div className="eyebrow">实时控制台</div>
          <h1>交易运行驾驶舱</h1>
          <p>实时状态、账户、风控和数据健康集中在这里；详细操作进入对应模块。</p>
        </div>
        <div className="header-status">
          <StatusPill status={connected ? "WS 在线" : "轮询/离线"} tone={connected ? "ok" : "warn"} />
          <StatusPill status={loopRunning ? "交易运行中" : "交易未运行"} tone={loopRunning ? "ok" : "warn"} />
          <StatusPill status={`接口 ${healthStatus}`} tone={toneFromStatus(healthStatus)} />
        </div>
      </div>

      <div className="stat-grid">
        <StatTile
          icon={Wallet}
          label="账户权益"
          value={formatMoney(equity, currency)}
          detail={`余额 ${formatMoney(balance, currency)} · ${currency}`}
          tone={equity > 0 ? "ok" : "mute"}
        />
        <StatTile
          icon={pnl >= 0 ? TrendingUp : TrendingDown}
          label="会话盈亏"
          value={formatMoney(pnl, currency)}
          detail={trades > 0 ? `${formatDecimal(trades, 0)} 笔 · 胜率 ${formatDecimal(winRate, 1)}%` : "今日暂无成交"}
          tone={numberTone(pnl)}
        />
        <StatTile
          icon={Activity}
          label="XAU 实时价"
          value={currentPrice > 0 ? formatDecimal(currentPrice, 2) : "暂无"}
          detail={hasSpread ? `买 ${formatDecimal(priceBid, 2)} · 卖 ${formatDecimal(priceAsk, 2)}` : currentPrice > 0 ? `XAUUSD+ · ${formatTime(serverTime)}` : "等待行情推送"}
          tone={currentPrice > 0 ? "ok" : "warn"}
        />
        <StatTile
          icon={ShieldCheck}
          label="风控状态"
          value={circuitBreaker ? "熔断触发" : "正常"}
          detail={`连续亏损 ${formatDecimal(consecutiveLoss, 0)} · DD ${formatDecimal(drawdown, 2)}%`}
          tone={circuitBreaker ? "bad" : consecutiveLoss >= 3 ? "warn" : "ok"}
        />
      </div>

      <div className="dashboard-grid">
        <MetricCard title="运行与行情">
          <div className="field-list">
            <Field label="状态" value={loopRunning ? "运行中" : "未运行"} tone={loopRunning ? "ok" : "warn"} />
            {hasMeaningfulText(broker) ? <Field label="经纪商" value={broker} /> : null}
            {hasMeaningfulText(strategy) ? <Field label="策略" value={strategy} /> : null}
            {executionMode ? <Field label="执行模式" value={executionMode} /> : null}
            {loopReason ? <Field label="原因" value={loopReason} /> : null}
            {hasMeaningfulText(loopStartedAt) ? <Field label="启动时间" value={formatTime(loopStartedAt)} /> : null}
            <Field label="现价" value={currentPrice > 0 ? formatDecimal(currentPrice, 2) : "暂无"} tone={currentPrice > 0 ? "ok" : "warn"} />
            {priceBid > 0 ? <Field label="买价" value={formatDecimal(priceBid, 2)} /> : null}
            {priceAsk > 0 ? <Field label="卖价" value={formatDecimal(priceAsk, 2)} /> : null}
            <Field label="行情状态" value={priceStatus} tone={currentPrice > 0 ? "ok" : "warn"} />
            {hasMeaningfulText(priceSource) ? <Field label="行情来源" value={translateDisplayValue(priceSource)} /> : null}
            <Field label="更新时间" value={serverTime ? formatTime(serverTime) : "--"} />
          </div>
        </MetricCard>

        <MetricCard title="账户与风控">
          <div className="field-list">
            <Field label="余额" value={formatMoney(balance, currency)} />
            <Field label="权益" value={formatMoney(equity, currency)} />
            {hasMarginData ? <Field label="已用保证金" value={formatMoney(margin, currency)} /> : null}
            {hasMarginData ? <Field label="可用保证金" value={formatMoney(marginFree, currency)} /> : null}
            {hasLeverageData ? <Field label="杠杆" value={leverage} /> : null}
            <Field label="熔断器" value={circuitBreaker ? "触发" : "未触发"} tone={circuitBreaker ? "bad" : "ok"} />
            {hasVarData ? (
              <Field
                label={varConfidence > 0 ? `VaR ${formatDecimal(varConfidence * 100, 0)}%` : "VaR"}
                value={`${formatDecimal(varValue, 2)}%`}
              />
            ) : null}
            {hasKellyData ? <Field label="Kelly" value={formatDecimal(kelly, 4)} /> : null}
            {consecutiveLoss > 0 ? <Field label="连续亏损" value={formatDecimal(consecutiveLoss, 0)} /> : null}
            {drawdown > 0 ? <Field label="回撤" value={`${formatDecimal(drawdown, 2)}%`} /> : null}
            {showDataHealth ? <Field label="数据库" value={dbStatus} tone={toneFromStatus(dbStatus)} /> : null}
            {dbProblems > 0 ? <Field label="数据库异常项" value={`${dbProblems}`} tone="bad" /> : null}
            {!readinessOk ? <Field label="后端就绪" value="否" tone="warn" /> : null}
            {blockers.length ? <Field label="阻断项" value={`${blockers.length} 项`} tone="bad" /> : null}
          </div>
        </MetricCard>
      </div>

      {apiErrors.length > 0 ? (
        <MetricCard title="接口异常">
          <ul className="error-list">
            {apiErrors.map(([name, err]) => (
              <li key={String(name)}>{String(name)}：{err instanceof Error ? err.message : "请求失败"}</li>
            ))}
          </ul>
        </MetricCard>
      ) : null}

      <div className="dashboard-footnote">
        数据源：{translateDisplayValue(source || "offline")} · 页面已改为结构化展示；需要排查请进入运维页查看详情。
      </div>
    </section>
  );
}

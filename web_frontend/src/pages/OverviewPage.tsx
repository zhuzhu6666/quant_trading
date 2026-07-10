import {
  Activity,
  ShieldCheck,
  TrendingDown,
  TrendingUp,
  Wallet,
} from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { MetricCard } from "@/components/Card";
import { CompactMetric as MiniMetric, Field, StatTile, numberTone, toneFromStatus, type Tone } from "@/components/DashboardBits";
import { StatusPill } from "@/components/StatusPill";
import { useAuth } from "@/contexts/AuthContext";
import { useLiveState } from "@/hooks/useLiveState";
import { useBackendReadinessQuery } from "@/hooks/useCoreQueries";
import {
  getAccount,
  getLoopStatus,
  getRiskSummary,
  getSessionStats,
} from "@/api/client";
import { getHealth, getLogTail, getSystemDbHealth } from "@/api/domains/system";
import { asRecord, pick, pickArray, pickBoolean, pickNumber, pickString } from "@/lib/compat";
import { translateDisplayValue } from "@/lib/display";
import { formatDecimal, formatMoney, formatTime } from "@/lib/format";

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
  const readiness = useBackendReadinessQuery();
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
    readiness,
    logs: useQuery({
      queryKey: ["logs-tail", "overview"],
      queryFn: () => getLogTail(24),
      refetchInterval: 5_000,
      staleTime: 2_000,
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
  const logPayload = asRecord(queries.logs.data);

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
  const spread = hasSpread ? Math.max(priceAsk - priceBid, 0) : 0;
  const positionFloating = positions.reduce<number>((sum, item) => {
    const row = asRecord(item);
    return sum + pickNumber(row, ["unrealized", "unrealized_pnl", "unrealized_profit", "pnl", "profit"], 0);
  }, 0);

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
  const problemDatabases = dbList.filter((item) => {
    const freshness = pickString(item, ["freshness", "status", "state"], "");
    const exists = pickBoolean(item, ["exists"], true);
    return !exists || ["missing", "stale", "old", "error"].includes(freshness) || pickArray(item, ["errors", "issues"]).length > 0;
  });
  const dbProblems = problemDatabases.length;
  const marginBase = margin + marginFree;
  const marginUsage = marginBase > 0 ? (margin / marginBase) * 100 : 0;
  const readinessText = readinessOk ? "就绪" : blockers.length ? `阻断 ${blockers.length}` : "未就绪";
  const healthBadges = [
    ...problemDatabases.slice(0, 4).map((item) => {
      const row = asRecord(item);
      return pickString(row, ["name", "database", "db", "path", "file"], "数据库异常");
    }),
    ...blockers.slice(0, 4).map((item) => translateDisplayValue(item)),
  ].filter((item) => hasMeaningfulText(item));
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
    ["日志接口", queries.logs.error],
  ].filter(([, err]) => Boolean(err));
  const backendLogLines = pickArray(logPayload, ["lines"]).map((line) => String(line)).filter((line) => line.trim()).slice(-80).reverse();
  const fallbackLogs = [
    {
      time: serverTime ? formatTime(serverTime) : formatTime(new Date().toISOString()),
      title: connected ? "WebSocket 实时流在线" : "WebSocket 未在线",
      detail: connected ? `实时数据源 ${translateDisplayValue(source || "websocket")}` : `当前使用 ${translateDisplayValue(source || "polling")}，等待实时流恢复`,
      tone: connected ? "ok" : "warn",
    },
    {
      time: hasMeaningfulText(loopStartedAt) ? formatTime(loopStartedAt) : serverTime ? formatTime(serverTime) : "--",
      title: loopRunning ? "交易循环运行中" : "交易循环未运行",
      detail: `${broker} · ${strategy}${executionMode ? ` · ${executionMode}` : ""}${loopReason ? ` · ${loopReason}` : ""}`,
      tone: loopRunning ? "ok" : "warn",
    },
    {
      time: serverTime ? formatTime(serverTime) : "--",
      title: currentPrice > 0 ? "行情报价已更新" : "等待行情报价",
      detail: currentPrice > 0 ? `XAU ${formatDecimal(currentPrice, 2)}${hasSpread ? ` · spread ${formatDecimal(spread, 2)}` : ""}` : "暂无有效现价",
      tone: currentPrice > 0 ? "ok" : "warn",
    },
    {
      time: serverTime ? formatTime(serverTime) : "--",
      title: circuitBreaker ? "风控熔断触发" : "风控检查正常",
      detail: `连续亏损 ${formatDecimal(consecutiveLoss, 0)} · 回撤 ${formatDecimal(drawdown, 2)}%`,
      tone: circuitBreaker ? "bad" : consecutiveLoss >= 3 ? "warn" : "ok",
    },
    {
      time: serverTime ? formatTime(serverTime) : "--",
      title: readinessOk ? "后端就绪检查通过" : "后端就绪受限",
      detail: blockers.length ? `阻断项 ${blockers.length}：${blockers.slice(0, 2).map((item) => translateDisplayValue(item)).join("；")}` : readinessText,
      tone: readinessOk ? "ok" : blockers.length ? "bad" : "warn",
    },
    {
      time: serverTime ? formatTime(serverTime) : "--",
      title: dbProblems > 0 ? "数据库健康存在异常" : "数据库健康正常",
      detail: `状态 ${translateDisplayValue(dbStatus)} · 数据库 ${formatDecimal(dbList.length, 0)} · 异常 ${dbProblems}`,
      tone: dbProblems > 0 ? "warn" : toneFromStatus(dbStatus),
    },
  ] satisfies Array<{ time: string; title: string; detail: string; tone: Tone }>;
  const realtimeLogs = backendLogLines.length ? backendLogLines : fallbackLogs.map((item) => `[${item.time}] ${item.title} - ${item.detail}`).reverse();

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
          <div className="overview-mini-grid">
            <MiniMetric label="持仓" value={formatDecimal(positionCount, 0)} detail={`浮盈 ${formatMoney(positionFloating, currency)}`} tone={numberTone(positionFloating)} />
            <MiniMetric label="买卖价差" value={hasSpread ? formatDecimal(spread, 2) : "--"} detail={hasSpread ? `${formatDecimal(priceBid, 2)} / ${formatDecimal(priceAsk, 2)}` : "等待报价"} tone={hasSpread ? "ok" : "warn"} />
            <MiniMetric label="执行模式" value={executionMode || "--"} detail={loopRunning ? "循环活跃" : "等待启动"} tone={loopRunning ? "ok" : "warn"} />
            <MiniMetric label="数据源" value={translateDisplayValue(source || "offline")} detail={connected ? "WS 实时" : "轮询/离线"} tone={connected ? "ok" : "warn"} />
          </div>
          <div className="field-list overview-field-list">
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
          <div className="overview-chip-row">
            <span className="data-badge">持仓 {formatDecimal(positionCount, 0)}</span>
            <span className={`data-badge ${currentPrice > 0 ? "data-badge-ok" : "data-badge-warn"}`}>行情 {priceStatus}</span>
            <span className={`data-badge ${loopRunning ? "data-badge-ok" : "data-badge-warn"}`}>循环 {loopRunning ? "运行" : "停止"}</span>
            {loopReason ? <span className="data-badge">{loopReason}</span> : null}
          </div>
        </MetricCard>

        <MetricCard title="账户与风控">
          <div className="overview-mini-grid">
            <MiniMetric label="保证金使用" value={hasMarginData ? `${formatDecimal(marginUsage, 1)}%` : "--"} detail={hasMarginData ? `${formatMoney(margin, currency)} / ${formatMoney(marginBase, currency)}` : "暂无保证金数据"} tone={marginUsage >= 70 ? "warn" : "ok"} />
            <MiniMetric label="今日交易" value={formatDecimal(trades, 0)} detail={`胜 ${formatDecimal(wins, 0)} / 负 ${formatDecimal(losses, 0)}`} tone={trades > 0 ? "ok" : "mute"} />
            <MiniMetric label="胜率" value={`${formatDecimal(winRate, 1)}%`} detail={`PnL ${formatMoney(pnl, currency)}`} tone={winRate >= 50 ? "ok" : trades > 0 ? "warn" : "mute"} />
            <MiniMetric label="数据健康" value={dbProblems > 0 ? `${dbProblems} 异常` : readinessText} detail={`库 ${formatDecimal(dbList.length, 0)} · ${dbStatus}`} tone={dbProblems > 0 || !readinessOk ? "warn" : "ok"} />
          </div>
          <div className="field-list overview-field-list">
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
          <div className="overview-chip-row">
            <span className={`data-badge ${circuitBreaker ? "data-badge-bad" : "data-badge-ok"}`}>熔断 {circuitBreaker ? "触发" : "未触发"}</span>
            <span className={`data-badge ${drawdown > 0 ? "data-badge-warn" : ""}`}>回撤 {formatDecimal(drawdown, 2)}%</span>
            {hasVarData ? <span className="data-badge">VaR {formatDecimal(varValue, 2)}%</span> : null}
            {hasKellyData ? <span className="data-badge">Kelly {formatDecimal(kelly, 4)}</span> : null}
            {healthBadges.map((item, index) => (
              <span className="data-badge data-badge-warn" key={`${item}-${index}`}>{item}</span>
            ))}
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

      <MetricCard title="实时日志" className="wide-panel overview-log-panel">
        <pre className="overview-log-scroll">{realtimeLogs.join("\n") || "暂无日志"}</pre>
      </MetricCard>

      <div className="dashboard-footnote">
        数据源：{translateDisplayValue(source || "offline")} · 页面已改为结构化展示；需要排查请进入运维页查看详情。
      </div>
    </section>
  );
}

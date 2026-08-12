import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Activity, Gauge, Play, PowerOff, RefreshCw, RotateCcw, ShieldAlert, Wallet } from "lucide-react";
import { ActionButton } from "@/components/ActionButton";
import { MetricCard } from "@/components/Card";
import { Field, StatTile, toneFromStatus } from "@/components/DashboardBits";
import { StatusPill } from "@/components/StatusPill";
import { FactBoundary } from "@/components/FactBoundary";
import { useAuth } from "@/contexts/AuthContext";
import { useLiveState } from "@/hooks/useLiveState";
import {
  emergencyClose,
  getFactorV4RecentTicks,
  getFactorV4Stats,
  isStepUpRequiredError,
  startTrading,
  stopTrading,
} from "@/api/client";
import { factAgeSeconds, factBoundTone, factHasDisplayValue, factIsKnown, factStatusLabel, readFactComponent, readFactNestedComponent } from "@/api/fact";
import { formatAgeSeconds, formatDecimal, formatMoney, formatTimeRange } from "@/lib/format";
import {
  asRecord,
  formatDirection,
  formatReadableTime,
  pick,
  pickArray,
  pickBoolean,
  pickNumber,
  pickString,
} from "@/lib/compat";
import { translateDisplayValue } from "@/lib/display";
import { decodeCanonicalRiskSnapshot, knownMetric } from "@/api/riskSnapshot";
import { RiskPanel } from "@/pages/RiskPage";

type PositionRow = {
  symbol: string;
  direction: string;
  volume: number;
  entry: number;
  current: number | null;
  unrealized: number | null;
  stop: number | null;
  take: number | null;
  source: string;
  id: string;
  openTs: unknown;
};

function optionalNumber(row: Record<string, unknown>, keys: string[]): number | null {
  const raw = pick(row, keys);
  if (raw === null || raw === undefined || raw === "") return null;
  const numeric = Number(raw);
  return Number.isFinite(numeric) ? numeric : null;
}

function componentValueAllowed(
  row: Record<string, unknown>,
  stateKeys: string[],
  aggregateDisplayable: boolean,
): boolean {
  if (!aggregateDisplayable) return false;
  const declared = pickString(row, stateKeys, "").trim().toLowerCase();
  return !declared || declared === "known" || declared === "stale";
}

function normalizePositions(
  raw: unknown,
  access: { protection: boolean; price: boolean; pnl: boolean },
): PositionRow[] {
  const list = pickArray(raw, ["positions_list", "positions", "items", "rows", "data", "payload"]);
  if (!list.length) return [];

  return list
    .map((item): PositionRow => {
      const row = asRecord(item);
      const id = pickString(row, ["position_id", "positionId", "ticket", "id", "deal_id", "order_id"], "");
      const priceAllowed = componentValueAllowed(
        row,
        ["current_price_state", "price_state"],
        access.price,
      );
      const pnlAllowed = componentValueAllowed(
        row,
        ["pnl_state", "unrealized_pnl_state"],
        access.pnl,
      );
      return {
        symbol: pickString(row, ["symbol", "instrument", "code", "asset"], ""),
        direction: formatDirection(pick(row, ["direction", "dir", "type", "side"])),
        volume: pickNumber(row, ["api_volume", "volume", "lots", "size", "positionSize", "qty"], 0),
        entry: pickNumber(row, ["price_open", "open_price", "entry_price", "open", "entry"], 0),
        current: priceAllowed
          ? optionalNumber(row, ["price_current", "current_price", "mark_price", "price", "last"])
          : null,
        unrealized: pnlAllowed
          ? optionalNumber(row, ["unrealized", "unrealized_pnl", "unrealized_profit", "pnl", "profit", "floating_pnl", "unrealized_pl"])
          : null,
        stop: access.protection
          ? optionalNumber(row, ["sl", "stop_loss", "stop", "stop_loss_price"])
          : null,
        take: access.protection
          ? optionalNumber(row, ["tp", "take_profit", "take", "take_profit_price"])
          : null,
        source: pickString(row, ["source", "origin", "route", "broker"], ""),
        id: id || pickString(row, ["position_id_str", "ticket_str"], ""),
        openTs: pick(row, ["open_time", "openTs", "open_timestamp", "openTsMs", "created_at", "opened_at"]),
      };
    })
    .filter((row) => row.symbol !== "" || row.id !== "");
}

function normalizePositionCount(raw: unknown): number {
  if (typeof raw === "number") return raw;
  const direct = pickNumber(raw, ["n_positions", "count", "position_count", "positions_count", "open_positions"], NaN);
  if (!Number.isNaN(direct)) return direct;
  return pickArray(raw, ["positions_list", "positions", "items", "rows", "data"]).length;
}

export function TradingPage() {
  const { authenticated } = useAuth();
  const {
    snapshot,
    connected,
    refresh,
    error: wsError,
    snapshotRequestFailed,
  } = useLiveState({ enabled: authenticated });
  const factorStatsQuery = useQuery({
    queryKey: ["factor-v4-stats", "trading"],
    queryFn: getFactorV4Stats,
    staleTime: 5_000,
    enabled: authenticated,
  });
  const recentTicksQuery = useQuery({
    queryKey: ["factor-v4-recent-ticks", "trading"],
    queryFn: getFactorV4RecentTicks,
    staleTime: 5_000,
    enabled: authenticated,
  });

  const [startError, setStartError] = useState<string | null>(null);
  const [stopError, setStopError] = useState<string | null>(null);
  const [closeError, setCloseError] = useState<string | null>(null);
  const [startBusy, setStartBusy] = useState(false);
  const [stopBusy, setStopBusy] = useState(false);
  const [closeBusy, setCloseBusy] = useState(false);
  const [stopRequested, setStopRequested] = useState(false);

  const loopFact = readFactComponent(snapshot, "loop", "live.loop.v2");
  const accountFact = readFactComponent(snapshot, "account", "live.account.v2");
  // The parent live.state envelope is an aggregate of broker fields that may
  // legitimately update at different rates. Market status follows the
  // heartbeat-owned loop fact so an unchanged position cannot age the whole
  // control header out of view.
  const statusFact = loopFact;
  const strategyFact = readFactComponent(snapshot, "strategy", "live.strategy.v2");
  const riskInputsFact = readFactComponent(snapshot, "risk_inputs", "risk.inputs.v1");
  const riskHealthFact = readFactComponent(snapshot, "risk_health", "system.runtime-health.v1");
  const accountComponentFact = readFactComponent(snapshot, "account", "live.account.v2");
  const positionsComponentFact = readFactComponent(snapshot, "positions", "live.positions.v2");
  const snapshotIdentityFact = readFactNestedComponent(snapshot, ["positions", "identity"], "live.positions.identity.v1");
  const snapshotProtectionFact = readFactNestedComponent(snapshot, ["positions", "protection"], "live.positions.protection.v1");
  const snapshotPriceFact = readFactNestedComponent(snapshot, ["positions", "price"], "live.positions.price.v1");
  const snapshotPnlFact = readFactNestedComponent(snapshot, ["positions", "pnl"], "live.positions.pnl.v1");
  const spotFact = readFactComponent(snapshot, "spot", "live.spot-quote.v1");
  const loopRequestFailed = snapshotRequestFailed;
  const statusRequestFailed = snapshotRequestFailed;
  const strategyRequestFailed = snapshotRequestFailed;
  const loopKnown = factIsKnown(loopFact, loopRequestFailed);
  const accountComponentKnown = factIsKnown(accountComponentFact, snapshotRequestFailed);
  const positionsComponentKnown = factIsKnown(positionsComponentFact, snapshotRequestFailed);
  const statusKnown = factIsKnown(statusFact, statusRequestFailed);
  const strategyKnown = factIsKnown(strategyFact, strategyRequestFailed);
  const riskRequestFailed = snapshotRequestFailed;
  const canonicalRisk = decodeCanonicalRiskSnapshot(snapshot);
  const riskKnown = factIsKnown(riskInputsFact, riskRequestFailed)
    && canonicalRisk.contractKnown
    && knownMetric(canonicalRisk.var95.status);
  const riskDisplayable = factHasDisplayValue(riskInputsFact, riskRequestFailed) && canonicalRisk.contractKnown;
  const riskVarDisplayable = riskDisplayable
    && knownMetric(canonicalRisk.var95.status)
    && canonicalRisk.var95.varPct !== null;
  const riskCvarDisplayable = riskDisplayable
    && knownMetric(canonicalRisk.var95.status)
    && canonicalRisk.var95.cvarPct !== null;
  const riskHealthKnown = factIsKnown(riskHealthFact, riskRequestFailed);

  const loop = asRecord(pick(snapshot, ["loop_status"]));
  const account = asRecord(pick(snapshot, ["account"]));
  const accountViewFact = accountComponentFact;
  const accountKnown = accountComponentKnown;
  const accountViewRequestFailed = snapshotRequestFailed;
  const snapshotIdentityDisplayable = factHasDisplayValue(snapshotIdentityFact, snapshotRequestFailed);
  const useEndpointPositions = false;
  const positionsViewFact = positionsComponentFact;
  const positionsIdentityFact = snapshotIdentityFact;
  const positionsProtectionFact = snapshotProtectionFact;
  const positionsPriceFact = snapshotPriceFact;
  const positionsPnlFact = snapshotPnlFact;
  const positionsViewRequestFailed = snapshotRequestFailed;
  const positionsIdentityDisplayable = factHasDisplayValue(positionsIdentityFact, positionsViewRequestFailed)
    || factIsKnown(positionsViewFact, positionsViewRequestFailed);
  const positionsProtectionDisplayable = factHasDisplayValue(positionsProtectionFact, positionsViewRequestFailed);
  const positionsPriceDisplayable = factHasDisplayValue(positionsPriceFact, positionsViewRequestFailed);
  const positionsPnlDisplayable = factHasDisplayValue(positionsPnlFact, positionsViewRequestFailed);
  const positions = useMemo(() => {
    const access = {
      protection: positionsProtectionDisplayable,
      price: positionsPriceDisplayable,
      pnl: positionsPnlDisplayable,
    };
    if (!useEndpointPositions && positionsIdentityDisplayable) {
      return normalizePositions(pick(snapshot, ["positions_list", "positions"]), access);
    }
    return [];
  }, [positionsIdentityDisplayable, positionsPnlDisplayable, positionsPriceDisplayable, positionsProtectionDisplayable, snapshot, useEndpointPositions]);

  const risk = asRecord(pick(snapshot, ["risk"]));
  const closedLoop = asRecord(pick(snapshot, ["closed_loop"]));
  const executionSummary = { ...closedLoop, ...asRecord(pick(snapshot, ["execution_summary", "execution"])) };
  const marketSession = asRecord(pick(snapshot, ["market_session"]));
  const spotQuote = factHasDisplayValue(spotFact, snapshotRequestFailed) ? asRecord(pick(snapshot, ["spot_quote"])) : {};
  const strategyStatus = asRecord(pick(snapshot, ["strategy_status"]));
  const lastComposite = asRecord(pick(strategyStatus, ["last_composite"]));
  const v4Status = asRecord(pick(strategyStatus, ["v4_status"]));
  const factorSummary = asRecord(pick(factorStatsQuery.data, ["summary"]));
  const topContributors = pickArray(factorSummary, ["top_contributors"]);
  const recentTicks = Array.isArray(recentTicksQuery.data) ? recentTicksQuery.data : pickArray(recentTicksQuery.data, ["items", "data"]);
  const factorTicks = useMemo(() => {
    const seen = new Set<string>();
    return recentTicks
      .map((raw, sourceOrder) => ({ item: asRecord(raw), sourceOrder }))
      .filter(({ item }) => pick(item, ["ts", "time"]) !== undefined)
      .sort((a, b) => {
        const timeDifference = pickNumber(b.item, ["ts"], 0) - pickNumber(a.item, ["ts"], 0);
        return timeDifference || b.sourceOrder - a.sourceOrder;
      })
      .filter(({ item }) => {
        const observedAt = pickNumber(item, ["ts"], 0);
        const fallbackTick = pickString(item, ["tick"], "");
        const key = observedAt > 0
          ? `ts:${observedAt}`
          : fallbackTick
            ? `tick:${fallbackTick}`
            : "";
        if (!key || seen.has(key)) {
          return false;
        }
        seen.add(key);
        return true;
      })
      .map(({ item }) => item);
  }, [recentTicks]);
  const latestFactorSignal = factorTicks[0] || {};
  const latestDecisionWindow = formatTimeRange(pick(latestFactorSignal, ["ts", "time"]), 5 * 60);
  const factorSnapshotAt = formatReadableTime(pick(lastComposite, ["ts"]));
  const executionEvents = pickArray(strategyStatus, ["execution_events"]);
  const liveExecutionSummary = { ...executionSummary, ...asRecord(pick(strategyStatus, ["execution_summary"])) };
  const strategyDisplayable = factHasDisplayValue(strategyFact, strategyRequestFailed);
  const executionKnown = strategyKnown;

  const spotKnown = factIsKnown(spotFact, snapshotRequestFailed);
  const startFactsKnown = loopKnown
    && accountKnown
    && positionsComponentKnown
    && statusKnown
    && riskKnown
    && riskHealthKnown
    && spotKnown;
  const positionsKnown = factIsKnown(positionsViewFact, positionsViewRequestFailed);

  const loopRunning = pickBoolean(loop, ["running", "is_running", "pipeline_active", "alive", "status"], false);
  const broker = pickString(loop, ["broker", "broker_name", "exchange"], pickString(account, ["broker"], ""));
  const strategy = pickString(loop, ["strategy_name", "strategy", "strategyName", "active_strategy"], "");
  const loopPhase = pickString(loop, ["phase"], loopRunning ? "running" : "stopped").trim().toLowerCase();
  const loopDraining = loopPhase === "draining" || pickBoolean(loop, ["draining"], false);
  const loopStopping = stopRequested || loopDraining;
  const loopDisplayLabel = loopStopping ? "停止中" : loopRunning ? "运行中" : "未运行";
  const loopDisplayStatus = `循环${loopDisplayLabel}`;
  const loopStatusTone = loopKnown && loopRunning && !loopStopping ? "ok" : "warn";
  const startDisabled = !loopKnown || loopRunning || loopDraining || stopRequested || startBusy;
  const startConfirmMessage = startFactsKnown
    ? `将使用 ${broker || "服务端配置"}${strategy ? ` · ${strategy}` : ""} 启动交易循环。请确认账户与风控状态正常。`
    : `当前部分账户、持仓、风险或行情事实尚未确认。提交后服务端会重新校验，未满足条件时将拒绝启动。将使用 ${broker || "服务端配置"}${strategy ? ` · ${strategy}` : ""}。`;
  const reason = pickString(loop, ["reason", "status", "stop_reason", "message", "state"], loopRunning ? "running" : "");
  const loopPid = pickNumber(loop, ["pid", "process_id", "pid_file"], 0);
  const loopStarted = formatReadableTime(pick(loop, ["started_at", "startedAt", "loop_started_at", "started", "start_time"]));
  const loopMode = pickString(loop, ["mode", "execution_mode", "send_mode"], "");

  const currency = pickString(account, ["currency", "ccy"], "");
  const balance = pickNumber(account, ["balance", "account_balance"], 0);
  const equity = pickNumber(account, ["equity", "account_equity"], 0);
  const leverage = pickString(account, ["leverage", "leverage_ratio"], "");
  const spotDisplayable = factHasDisplayValue(spotFact, snapshotRequestFailed);
  const spotMid = spotDisplayable ? pickNumber(spotQuote, ["mid"], pickNumber(snapshot, ["current_price"], 0)) : 0;
  const spotBid = spotDisplayable ? pickNumber(spotQuote, ["bid"], 0) : 0;
  const spotAsk = spotDisplayable ? pickNumber(spotQuote, ["ask"], 0) : 0;
  const spotObservedAt = spotFact.observed_at ? formatReadableTime(spotFact.observed_at) : "";
  const spotAgeLabel = formatAgeSeconds(spotDisplayable ? factAgeSeconds(spotFact) : null);
  const spotTone = factBoundTone(spotFact, spotMid > 0 ? "ok" : "warn", snapshotRequestFailed);
  const marketStatus = pickString(marketSession, ["status"], "");

  const positionsDisplayable = positionsIdentityDisplayable;
  const positionCount = positions.length;
  const cumulativeVolume = positions.reduce((sum, p) => sum + Math.abs(p.volume), 0);
  const pnlValues = positions.flatMap((position) => position.unrealized === null ? [] : [position.unrealized]);
  const pnlComplete = positionsPnlDisplayable && pnlValues.length === positions.length;
  const unrealized = pnlComplete ? pnlValues.reduce((sum, value) => sum + value, 0) : null;
  const bestUnrealized = pnlComplete && pnlValues.length ? Math.max(...pnlValues) : null;
  const worstUnrealized = pnlComplete && pnlValues.length ? Math.min(...pnlValues) : null;
  const pnlObservedAt = positionsPnlFact.observed_at ? formatReadableTime(positionsPnlFact.observed_at) : "";

  const session = asRecord(pick(snapshot, ["session_stats", "daily", "session"]));
  const sessionPnl = pickNumber(session, ["pnl_today", "pnl", "session_pnl"], 0);
  const riskSystemHealth = asRecord(pick(snapshot, ["risk_health"]));
  const totalRisk = riskVarDisplayable ? canonicalRisk.var95.varPct : null;
  const circuitBreaker = riskHealthKnown && pickBoolean(riskSystemHealth, ["trading_blocked"], false);
  const riskFactLabel = factStatusLabel(riskInputsFact, riskRequestFailed);
  const riskFactTone = factBoundTone(riskInputsFact, riskKnown ? "mute" : "warn", riskRequestFailed);
  const riskGateLabel = !riskHealthKnown
    ? "未知"
    : circuitBreaker
      ? "阻断"
      : riskKnown
        ? "已知"
        : riskDisplayable && riskInputsFact.state === "stale"
          ? "已过期"
          : "待确认";
  const riskGateTone = circuitBreaker ? "bad" : riskHealthKnown && riskKnown ? "ok" : "warn";
  const riskGateDetail = riskCvarDisplayable && totalRisk !== null
    ? `VaR ${formatDecimal(totalRisk, 4)}% · CVaR ${formatDecimal(canonicalRisk.var95.cvarPct, 4)}%${riskInputsFact.state === "stale" ? ` · 最后观测 ${formatReadableTime(riskInputsFact.observed_at)}` : ""}`
    : `风险事实 ${riskFactLabel}`;
  const consecutiveLoss = pickNumber(session, ["consecutive_loss", "session_consecutive_loss"], 0);
  const attempts = pickNumber(liveExecutionSummary, ["attempts", "attempt_count"], 0);
  const successes = pickNumber(liveExecutionSummary, ["successes", "success", "wire_sends"], 0);
  const failures = pickNumber(liveExecutionSummary, ["failures", "reject_count"], 0);
  const signalDirection = formatDirection(pick(lastComposite, ["direction"]));
  const signalScore = pickNumber(lastComposite, ["score"], 0);
  const gatePassed = pickBoolean(lastComposite, ["gate_passed"], false);
  const gateReason = pickString(lastComposite, ["gate_reason"], pickString(strategyStatus, ["reason"], ""));
  const engineWarm = pickBoolean(v4Status, ["engine_warm"], false);
  const bufferSize = pickNumber(v4Status, ["buffer_size"], 0);
  const aweConviction = pickNumber(v4Status, ["awe_conviction"], 0);
  const attributedTrades = pickNumber(v4Status, ["n_attribution_trades"], pickNumber(factorSummary, ["total_voted"], 0));
  const overallWinRate = pickNumber(factorSummary, ["overall_win_rate"], 0);
  const hasLoopData = factHasDisplayValue(loopFact, loopRequestFailed) && Object.keys(loop).length > 0;
  const hasAccountData = factHasDisplayValue(accountViewFact, accountViewRequestFailed) && pick(account, ["balance", "equity"]) !== undefined;

  useEffect(() => {
    if (stopRequested && loopKnown && !loopRunning && !loopDraining) {
      setStopRequested(false);
    }
  }, [loopDraining, loopKnown, loopRunning, stopRequested]);
  const hasPositionData = positionsDisplayable;

  const refreshAll = async () => {
    await refresh();
  };

  const runStart = async () => {
    setStartError(null);
    setStartBusy(true);
    try {
      await startTrading("ctrader", strategy || "live", true);
      setStopRequested(false);
      await refreshAll();
    } catch (exc) {
      if (!isStepUpRequiredError(exc)) {
        setStartError(exc instanceof Error ? exc.message : "启动失败");
      }
      throw exc;
    } finally {
      setStartBusy(false);
    }
  };

  const runStop = async () => {
    setStopError(null);
    setStopBusy(true);
    try {
      const stopResult = await stopTrading();
      if (pickBoolean(stopResult, ["ok"], true)) {
        setStopRequested(true);
      }
      await refreshAll();
    } catch (exc) {
      setStopRequested(false);
      setStopError(exc instanceof Error ? exc.message : "停止失败");
    } finally {
      setStopBusy(false);
    }
  };

  const runEmergency = async () => {
    setCloseError(null);
    setCloseBusy(true);
    try {
      await emergencyClose(true);
      await refreshAll();
    } catch (exc) {
      setCloseError(exc instanceof Error ? exc.message : "紧急平仓失败");
    } finally {
      setCloseBusy(false);
    }
  };

  return (
    <section className="dashboard trading-risk-dashboard">
      <div className="dashboard-header">
        <div>
          <div className="eyebrow">交易与风控</div>
          <h1>交易控制与风险中枢</h1>
          <p>循环控制、持仓、开仓门控和权威风险事实集中展示，所有数值以实时接口为准。</p>
        </div>
        <div className="header-status">
          <StatusPill
            status={connected && snapshot ? "WS 实时连接" : "暂无实时快照"}
            tone={connected && snapshot ? "ok" : "warn"}
          />
          {hasLoopData ? <StatusPill status={loopDisplayStatus} tone={loopStatusTone} fact={loopFact} requestFailed={loopRequestFailed} /> : <StatusPill status="循环状态未知" tone="warn" fact={loopFact} requestFailed={loopRequestFailed} />}
          <StatusPill status={factHasDisplayValue(statusFact, statusRequestFailed) ? `市场 ${translateDisplayValue(marketStatus)}` : "市场状态未知"} tone={factBoundTone(statusFact, toneFromStatus(marketStatus), statusRequestFailed)} fact={statusFact} requestFailed={statusRequestFailed} />
          <StatusPill status={broker} tone="mute" />
        </div>
      </div>

      <div className="trading-toolbar">
        <div className="trading-actions" aria-label="交易操作">
          <ActionButton icon={Play} label="启动" variant="primary" disabled={startDisabled} loading={startBusy} error={startError} confirmTitle="确认启动实盘交易" confirmMessage={startConfirmMessage} stepUpOnDemand onAction={runStart} />
          <ActionButton icon={PowerOff} label={loopStopping ? "停止中" : "停止"} variant="danger" disabled={stopBusy || stopRequested || loopDraining} loading={stopBusy} error={stopError} confirmTitle="确认停止交易循环" confirmMessage="停止请求在状态未知时仍可执行；停止后不再产生新订单，已有持仓不会因此自动平仓。" onAction={runStop} />
          <ActionButton icon={RotateCcw} label="紧急平仓" variant="danger" disabled={closeBusy} loading={closeBusy} error={closeError} confirmTitle="确认紧急平仓" confirmMessage={positionsKnown && unrealized !== null ? `服务端将严格对账当前 ${formatDecimal(positionCount, 0)} 个持仓，当前浮动盈亏 ${formatMoney(unrealized, currency)}。该操作不可撤销。` : "当前持仓事实或浮盈事实未知；紧急操作仍可提交，服务端会先锁定 no_new_risk 并执行 fresh broker 对账。"} onAction={runEmergency} />
          <button className="action-btn action-ghost refresh-inline" type="button" onClick={() => void refreshAll()}>
            <RefreshCw size={15} />
            <span>刷新</span>
          </button>
        </div>
        <FactBoundary fact={accountViewFact} label="账户事实" requestFailed={accountViewRequestFailed}>
          <div className="toolbar-account-strip">
            <span>币种 <strong>{currency}</strong></span>
            <span>余额 <strong>{formatMoney(balance, currency)}</strong></span>
            <span>权益 <strong>{formatMoney(equity, currency)}</strong></span>
            <span>杠杆 <strong>{leverage}</strong></span>
          </div>
        </FactBoundary>
        {wsError ? <span className="toolbar-error">WS：{wsError}</span> : null}
      </div>

      <div className="stat-grid">
        {hasLoopData ? <StatTile icon={Activity} label="交易循环" value={loopDisplayLabel} detail={translateDisplayValue(reason)} tone={loopStatusTone} fact={loopFact} requestFailed={loopRequestFailed} /> : null}
        {hasAccountData ? <StatTile icon={Wallet} label="账户权益" value={formatMoney(equity, currency)} detail={`余额 ${formatMoney(balance, currency)}`} tone={equity > 0 ? "ok" : "mute"} fact={accountViewFact} requestFailed={accountViewRequestFailed} /> : null}
        <StatTile icon={Gauge} label="XAU 最新报价" value={spotMid > 0 ? formatDecimal(spotMid, 2) : "未知"} detail={spotBid && spotAsk ? `买 ${formatDecimal(spotBid, 2)} · 卖 ${formatDecimal(spotAsk, 2)} · 最后观测 ${spotAgeLabel}` : spotObservedAt ? `最后观测 ${spotAgeLabel}` : "等待 spot 事实"} tone={spotMid > 0 ? "ok" : "warn"} fact={spotFact} requestFailed={snapshotRequestFailed} />
        {hasPositionData ? <StatTile icon={ShieldAlert} label="浮动盈亏" value={unrealized === null ? "未知" : formatMoney(unrealized, currency)} detail={`${positionsPnlFact.state === "stale" && pnlObservedAt ? `最后观测 ${pnlObservedAt} · ` : ""}会话 ${formatMoney(sessionPnl, currency)}`} tone={unrealized === null ? "warn" : unrealized > 0 ? "ok" : unrealized < 0 ? "bad" : "mute"} fact={positionsPnlFact} requestFailed={positionsViewRequestFailed} /> : null}
        <StatTile icon={Gauge} label="CVaR 95%" value={riskCvarDisplayable ? `${formatDecimal(canonicalRisk.var95.cvarPct, 4)}%` : riskFactLabel} detail={riskGateDetail} tone={riskKnown ? "mute" : "warn"} fact={riskInputsFact} requestFailed={riskRequestFailed} />
        <StatTile icon={ShieldAlert} label="风险面" value={riskGateLabel} detail={riskGateDetail} tone={riskGateTone} fact={riskHealthFact} requestFailed={riskRequestFailed} />
      </div>

      <MetricCard title="运行总览" className="wide-panel trading-status-overview">
        <div className="trading-status-grid">
          <section className="trading-status-section" aria-label="循环摘要">
            <div className="trading-status-head">
              <h3>循环摘要</h3>
              <StatusPill status={loopDisplayLabel} tone={loopStatusTone} fact={loopFact} requestFailed={loopRequestFailed} />
            </div>
            <div className="field-list trading-compact-fields">
              <Field label="经纪商" value={broker} />
              <Field label="策略" value={strategy} />
              <Field label="执行模式" value={translateDisplayValue(loopMode)} />
              <Field label="PID" value={loopPid || ""} />
              <Field label="启动时间" value={loopStarted} />
              <Field label="状态说明" value={reason} tone={factBoundTone(loopFact, toneFromStatus(reason), loopRequestFailed)} />
            </div>
          </section>

          <section className="trading-status-section" aria-label="策略信号">
            <div className="trading-status-head">
              <h3>策略信号</h3>
              <StatusPill status={strategyDisplayable ? (gatePassed ? "信号通过" : "信号未通过") : "状态未知"} tone={factBoundTone(strategyFact, gatePassed ? "ok" : "warn", strategyRequestFailed)} fact={strategyFact} requestFailed={strategyRequestFailed} />
            </div>
            <div className="field-list trading-compact-fields">
              <Field label="实单发送" value={strategyDisplayable ? (pickBoolean(strategyStatus, ["send_orders"], false) ? "开启" : "关闭") : "未知"} tone={factBoundTone(strategyFact, pickBoolean(strategyStatus, ["send_orders"], false) ? "ok" : "warn", strategyRequestFailed)} />
              <Field label="执行模式" value={translateDisplayValue(pickString(strategyStatus, ["execution_mode", "mode"], loopMode))} />
              <Field label="当前方向" value={translateDisplayValue(signalDirection)} />
              <Field label="综合分" value={formatDecimal(signalScore, 4)} />
              <Field label="决策K线" value={latestDecisionWindow ? `${latestDecisionWindow} · 已收盘` : "未知"} />
              <Field label="决策条件原因" value={translateDisplayValue(gateReason)} />
            </div>
          </section>

          <section className="trading-status-section" aria-label="因子管道">
            <div className="trading-status-head">
              <h3>因子管道</h3>
              <StatusPill status={strategyDisplayable ? (engineWarm ? "已预热" : "预热中") : "状态未知"} tone={factBoundTone(strategyFact, engineWarm ? "ok" : "warn", strategyRequestFailed)} fact={strategyFact} requestFailed={strategyRequestFailed} />
            </div>
            <div className="field-list trading-compact-fields">
              <Field label="缓冲区" value={formatDecimal(bufferSize, 0)} />
              <Field label="可用因子" value={formatDecimal(pickNumber(lastComposite, ["n_available", "n_available_factors"], 0), 0)} />
              <Field label="评分因子" value={formatDecimal(pickNumber(lastComposite, ["n_scoring", "n_scoring_factors"], 0), 0)} />
              <Field label="贡献因子" value={formatDecimal(pickNumber(lastComposite, ["n_contributing", "n_contributing_factors"], 0), 0)} />
              <Field label="弃权因子" value={formatDecimal(pickNumber(lastComposite, ["n_abstain", "n_abstain_factors"], 0), 0)} />
              <Field label="因子快照生成" value={factorSnapshotAt || "未知"} />
              <Field label="归因样本" value={formatDecimal(attributedTrades, 0)} />
              <Field label="自适应综合置信度" value={formatDecimal(aweConviction * 100, 1) + "%"} />
              <Field label="归因胜率" value={overallWinRate ? `${formatDecimal(overallWinRate * 100, 1)}%` : ""} />
            </div>
            <div className="compact-list trading-inline-badges">
              {topContributors.slice(0, 4).map((raw, index) => {
                const item = asRecord(raw);
                const factor = pickString(item, ["factor", "name"], String(index + 1));
                const value = pickNumber(item, ["pnl", "contribution", "score", "gross"], 0);
                return <span className="data-badge" key={`${factor}-${index}`}>{factor}: {formatDecimal(value, 2)}</span>;
              })}
            </div>
          </section>

          <section className="trading-status-section" aria-label="风控与执行">
            <div className="trading-status-head">
              <h3>开仓门控与执行</h3>
              <StatusPill
                status={riskHealthKnown ? (circuitBreaker ? "健康面阻断" : riskKnown ? "风险输入已知" : riskDisplayable && riskInputsFact.state === "stale" ? "风险输入已过期" : "风险输入待确认") : "状态未知"}
                tone={circuitBreaker ? "bad" : riskHealthKnown && riskKnown ? "ok" : "warn"}
                fact={riskInputsFact}
                requestFailed={riskRequestFailed}
              />
            </div>
            <div className="field-list trading-compact-fields">
              <Field label="连续亏损" value={formatDecimal(consecutiveLoss, 0)} tone={consecutiveLoss >= 3 ? "warn" : "mute"} />
              <Field label="前瞻 VaR 95%" value={totalRisk === null ? riskFactLabel : `${formatDecimal(totalRisk, 4)}%`} tone={riskFactTone} />
              <Field label="前瞻 CVaR 95%" value={riskCvarDisplayable ? `${formatDecimal(canonicalRisk.var95.cvarPct, 4)}%` : riskFactLabel} tone={riskFactTone} />
              <Field label="执行尝试" value={formatDecimal(attempts, 0)} />
              <Field label="下单成功" value={formatDecimal(successes, 0)} tone={executionKnown && successes > 0 ? "ok" : successes > 0 ? "pending" : "mute"} />
              <Field label="失败" value={formatDecimal(failures, 0)} tone={failures > 0 ? "bad" : executionKnown ? "ok" : "pending"} />
              <Field label="最近执行" value={translateDisplayValue(pickString(liveExecutionSummary, ["last_reason", "last_reason_text", "lastReason"], ""))} />
            </div>
            <div className="compact-list trading-inline-badges">
              {executionEvents.slice(0, 3).map((raw, index) => {
                const item = asRecord(raw);
                const stage = pickString(item, ["stage"], "");
                return (
                  <span className={`data-badge ${stage === "success" && strategyKnown ? "data-badge-ok" : stage === "failure" ? "data-badge-bad" : "data-badge-warn"}`} key={`${pickString(item, ["time"], String(index))}-${index}`}>
                    {translateDisplayValue(stage)} · {translateDisplayValue(formatDirection(pick(item, ["direction"])))}
                  </span>
                );
              })}
            </div>
          </section>
        </div>
      </MetricCard>

      <RiskPanel
        riskData={risk}
        riskRequestFailed={riskRequestFailed}
        embedded
        factorSignals={factorTicks}
        factorSignalsPending={recentTicksQuery.isPending && !recentTicksQuery.data}
        factorSignalsRequestFailed={recentTicksQuery.isError || recentTicksQuery.isRefetchError}
        currentPositionIds={positions.map((item) => item.id)}
        currentPositionsKnown={positionsKnown}
        positionsContent={(
          <MetricCard title="持仓表" className="risk-audit-card risk-position-card">
        <div className="table-wrap">
          <table className="mobile-card-table positions-table">
            <thead>
              <tr>
                <th scope="col">品种</th>
                <th scope="col">方向</th>
                <th scope="col">数量</th>
                <th scope="col">开仓价</th>
                <th scope="col">当前价</th>
                <th scope="col">浮盈</th>
                <th scope="col">止损</th>
                <th scope="col">止盈</th>
                <th scope="col">持仓ID</th>
                <th scope="col">来源</th>
                <th scope="col">开仓时间</th>
              </tr>
            </thead>
            <tbody>
              {!positions.length ? (
                <tr>
                  <td colSpan={11} className="empty-state-small">当前无持仓</td>
                </tr>
              ) : null}
              {positions.map((item) => (
                <tr key={`${item.id}-${item.symbol}-${String(item.openTs || "")}`}>
                  <td>{item.symbol}</td>
                  <td>
                    <span className={`status-pill ${item.direction === "LONG" && positionsKnown ? "status-ok" : item.direction === "SHORT" ? "status-bad" : "status-neutral"}`}>
                      {translateDisplayValue(item.direction)}
                    </span>
                  </td>
                  <td>{formatDecimal(item.volume, 2)}</td>
                  <td>{item.entry ? formatDecimal(item.entry, 5) : ""}</td>
                  <td>{item.current === null ? "未知" : formatDecimal(item.current, 5)}</td>
                  <td className={item.unrealized !== null && item.unrealized >= 0 && factIsKnown(positionsPnlFact, positionsViewRequestFailed) ? "status-ok" : item.unrealized !== null && item.unrealized < 0 ? "status-bad" : "status-neutral"}>{item.unrealized === null ? "未知" : formatMoney(item.unrealized, currency)}</td>
                  <td>{item.stop === null ? "未知" : formatDecimal(item.stop, 5)}</td>
                  <td>{item.take === null ? "未知" : formatDecimal(item.take, 5)}</td>
                  <td>{item.id}</td>
                  <td>{item.source}</td>
                  <td>{formatReadableTime(item.openTs)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {positions.length ? (
          <p className="summary-note">{pnlComplete && worstUnrealized !== null && bestUnrealized !== null ? `浮盈区间：${formatMoney(worstUnrealized, currency)} 到 ${formatMoney(bestUnrealized, currency)}` : `浮盈事实${positionsPnlFact.state === "error" ? "读取错误" : "未知"}${pnlObservedAt ? ` · 最后观测 ${pnlObservedAt}` : ""}`}</p>
        ) : null}
          </MetricCard>
        )}
      />

      {wsError || factorStatsQuery.isError || recentTicksQuery.isError ? (
        <MetricCard title="错误状态" className="wide-panel">
          <ul className="error-list">
            {wsError ? <li>WS：{wsError}</li> : null}
            {factorStatsQuery.isError ? <li>factor-stats：{factorStatsQuery.error instanceof Error ? factorStatsQuery.error.message : "请求失败"}</li> : null}
            {recentTicksQuery.isError ? <li>recent-ticks：{recentTicksQuery.error instanceof Error ? recentTicksQuery.error.message : "请求失败"}</li> : null}
          </ul>
        </MetricCard>
      ) : null}
    </section>
  );
}

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BrainCircuit, Clock3, Copy, Crosshair, FlaskConical, Play, Search } from "lucide-react";
import type { FactEnvelope } from "@/api/fact";
import { epochSeconds, formatObservedTime, formatTimestamp } from "@/api/time";
import { getFactorCatalogSnapshot, getLearningResearchSnapshot, getMarketBars, getReplayDecisionTrace, getReplaySnapshot, runReplay } from "@/api/workbench";
import { putResearchCache } from "@/cache/researchCache";
import { readCachedMarketBars, readCachedResearchSnapshot } from "@/cache/researchFallback";
import { MarketChart } from "@/design-system/MarketChart";
import { FactBadge, Panel, SourceLine } from "@/design-system/primitives";
import { useNetworkStatus } from "@/hooks/useNetworkStatus";
import type { DecisionTrace, ResearchRow, ResearchSnapshot } from "@/types/contracts";
import { WorkspaceTitle } from "@/workspaces/WorkspaceBits";
import { uiStatus } from "@/i18n/zh-CN";

const unavailableFact = (contract: string, reasonCode: string, state: FactEnvelope["state"] = "unknown"): FactEnvelope => ({ envelope: "fact.v1", contract, state, source: "none", observed_at: null, generated_at: null, stale_after_sec: 0, reason_code: reasonCode, components: {} });
const queryFact = (fact: FactEnvelope | undefined, error: unknown, contract: string, reasonCode: string): FactEnvelope => fact ?? unavailableFact(contract, error ? `${reasonCode}_request_failed` : reasonCode, error ? "error" : "unknown");
const readableFact = (fact: FactEnvelope): boolean => fact.state === "known" || fact.state === "stale";
const WINDOW_BAR_OPTIONS = [16, 24, 40] as const;
const TIMEFRAME_SECONDS: Readonly<Record<string, number>> = { M5: 300, M15: 900, M30: 1_800, H1: 3_600, H4: 14_400, D1: 86_400 };

function timeframeSeconds(value: string | null | undefined): number | null {
  if (!value) return null;
  return TIMEFRAME_SECONDS[value.toUpperCase()] ?? null;
}

function directionLabel(value: string | null | undefined): string {
  const normalized = value?.toLowerCase();
  if (normalized === "long" || normalized === "buy" || normalized === "1" || normalized === "direction_long") return "多";
  if (normalized === "short" || normalized === "sell" || normalized === "-1" || normalized === "2" || normalized === "direction_short") return "空";
  return "方向未知";
}

function outcomeLabel(item: DecisionTrace): string {
  if (item.outcomeLabel) return uiStatus(item.outcomeLabel);
  if (item.outcomeStatus) return uiStatus(item.outcomeStatus);
  return "后验待确认";
}

function pnlLabel(value: number | null): string {
  if (value === null) return "盈亏未知";
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}`;
}

function entryTimestamp(item: DecisionTrace | undefined): number {
  return item?.entryTs ?? epochSeconds(item?.observedAt);
}

function exitTimestamp(item: DecisionTrace | undefined): number {
  return item?.exitTs ?? 0;
}

function holdingDurationLabel(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "持仓时长未知";
  const totalMinutes = Math.floor(seconds / 60);
  if (totalMinutes < 60) return `持仓 ${totalMinutes} 分钟`;
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  return `持仓 ${hours}小时${minutes ? ` ${minutes}分` : ""}`;
}

function scoreLabel(value: number | null): string {
  if (value === null) return "评分未知";
  return `评分 ${value >= 0 ? "+" : ""}${value.toFixed(3)}`;
}

function ChartSystemView({ item }: { item: DecisionTrace }) {
  const view = item.systemView;
  const score = view?.score ?? item.actionScore ?? null;
  const actionReason = view?.actionReason ?? item.actionReason;
  const outcomeStatus = view?.outcomeStatus ?? item.outcomeStatus;
  const pnl = view?.pnl ?? item.pnl ?? null;
  const closeReason = view?.closeReason ?? item.closeReason;
  const summary = view?.summary;
  const hasView = Boolean(view || actionReason || score !== null || outcomeStatus || closeReason || summary);
  if (!hasView) return <div className="chart-system-view chart-system-view-empty"><BrainCircuit size={14} /><span>服务端暂未提供本次仓位的系统观点。</span></div>;
  return <div className="chart-system-view">
    <div className="chart-system-view-head"><span><BrainCircuit size={14} /><strong>系统观点</strong></span><small>服务端决策投影</small></div>
    <div className="chart-system-view-grid">
      <span><b>入场判断</b>{directionLabel(view?.direction ?? item.direction)} · {scoreLabel(score)}</span>
      <span><b>执行动作</b>{actionReason || "动作理由未知"}</span>
      <span><b>后验结果</b>{outcomeStatus === "closed" ? `${outcomeLabel(item)} · ${pnlLabel(pnl)}` : "尚未平仓"}</span>
      {closeReason && <span><b>平仓原因</b>{closeReason}</span>}
    </div>
    {summary && <p>{summary}</p>}
  </div>;
}

function ResearchRows({ rows, emptyMessage = "证据结果为空；这不是成功或失败的替代值。", variant = "factor" }: { rows: ResearchRow[]; emptyMessage?: string; variant?: "factor" | "learning" }) {
  if (!rows.length) return <div className="empty-confirmed">{emptyMessage}</div>;
  return <div className={`research-row-list research-row-list-${variant}`}>{rows.slice(0, 12).map((row) => <div className={`research-row research-row-${variant}`} key={row.id}>
    <div className="research-row-main">
      <span className="research-row-id" title={row.id}>{row.id}</span>
      {row.title !== row.id && <strong title={row.title}>{row.title}</strong>}
      <span className="research-row-state">{uiStatus(row.state)}</span>
    </div>
    <div className="research-row-meta">
      {row.reasonCode && <code title={row.reasonCode}>{row.reasonCode}</code>}
      {row.detail && <small title={row.detail}>{row.detail}</small>}
      <time>{formatObservedTime(row.observedAt)}</time>
    </div>
  </div>)}</div>;
}

function DecisionTraceRows({ items, emptyMessage = "证据结果为空；这不是成功或失败的替代值。" }: { items: DecisionTrace[]; emptyMessage?: string }) {
  if (!items.length) return <div className="empty-confirmed">{emptyMessage}</div>;
  return <div className="research-row-list decision-trace-list">{items.slice(0, 12).map((item) => {
    const title = [item.eventType, item.symbol, item.timeframe].filter(Boolean).join(" · ") || "决策记录";
    const reason = item.actionReason ?? item.reasonCode ?? item.outcomeResult;
    const outcome = [item.outcomeStatus, item.outcomeLabel, item.learningStatus].filter(Boolean).map((value) => uiStatus(value)).join(" · ");
    return <div className="research-row decision-trace-row" key={item.traceId}>
      <div className="decision-trace-row-main">
        <span className="research-row-id" title={item.decisionId ?? item.traceId}>{item.decisionId ?? item.traceId}</span>
        <strong title={title}>{title}</strong>
        <span className="decision-trace-position" title={item.positionId ? `仓位 ${item.positionId}` : "未关联仓位"}>{item.positionId ? `仓位 ${item.positionId}` : "未关联仓位"}</span>
      </div>
      <div className="decision-trace-row-meta">
        {reason && <code title={reason}>{reason}</code>}
        {outcome && <small title={outcome}>{outcome}</small>}
        <time>{formatObservedTime(item.observedAt)}</time>
      </div>
    </div>;
  })}</div>;
}

function ReplayPositionTimeline({ items, selectedDecisionId, onSelect }: { items: DecisionTrace[]; selectedDecisionId?: string; onSelect: (decisionId: string) => void }) {
  if (!items.length) return <div className="empty-confirmed">暂无同时具备入场和出场事实的历史仓位；未显示未闭合或时间未知记录。</div>;
  return <div className="replay-position-list" role="listbox" aria-label="历史仓位时间线">
    {items.map((item) => {
      const decisionId = item.decisionId as string;
      const selected = decisionId === selectedDecisionId;
      const pnl = item.pnl ?? null;
      const positivePnl = pnl !== null && pnl >= 0;
      const entryTs = entryTimestamp(item);
      const exitTs = exitTimestamp(item);
      const holdingSeconds = item.holdingSeconds ?? (exitTs > entryTs ? exitTs - entryTs : 0);
      return <button key={decisionId} type="button" role="option" aria-selected={selected} className={`replay-position-item ${selected ? "replay-position-item-selected" : ""}`} onClick={() => onSelect(decisionId)}>
        <span className="replay-position-rail" aria-hidden="true"><span className="replay-position-dot" /></span>
        <span className="replay-position-copy">
          <span className="replay-position-head"><strong>{item.symbol ?? "品种未知"} · {directionLabel(item.direction)}</strong><span className="replay-position-status">已平仓</span></span>
          <span className="replay-position-times"><time>入 {formatTimestamp(entryTs, "时间未知")}</time><time>出 {formatTimestamp(exitTs, "时间未知")}</time></span>
          <span className="replay-position-meta"><span>仓位 {item.positionId}</span><span>{item.timeframe ?? "周期未知"}</span><span>{holdingDurationLabel(holdingSeconds)}</span><span className={pnl === null ? "" : positivePnl ? "text-positive" : "text-negative"}>{pnlLabel(pnl)}</span></span>
          <span className="replay-position-outcome"><span>{outcomeLabel(item)}</span>{item.actionReason && <small>{item.actionReason}</small>}</span>
        </span>
        <Crosshair size={14} className="replay-position-select-icon" aria-hidden="true" />
      </button>;
    })}
  </div>;
}

function EvidenceLink({ label, id }: { label: string; id: string | null }) {
  const copy = () => { if (id) void navigator.clipboard?.writeText(id); };
  return <button type="button" className="evidence-link" disabled={!id} onClick={copy}><Copy size={13} />{label}: {id ?? "reference unknown"}</button>;
}

async function fromCache<T>(read: () => Promise<T | null>): Promise<T | null> {
  try { return await read(); } catch { return null; }
}

async function marketWithOfflineCache(): Promise<Awaited<ReturnType<typeof getMarketBars>>> {
  try { return await getMarketBars("XAUUSD+", "M15", 240); } catch (error) {
    const cached = await fromCache(() => readCachedMarketBars("market:XAUUSD+:M15", "XAUUSD+", "M15"));
    if (cached) return cached;
    throw error;
  }
}

async function factorWithOfflineCache(): Promise<ResearchSnapshot> {
  try { return await getFactorCatalogSnapshot(); } catch (error) {
    const cached = await fromCache(() => readCachedResearchSnapshot("factor:catalog:latest", "factor.catalog.v4"));
    if (cached) return cached;
    throw error;
  }
}

async function replayWithOfflineCache(): Promise<ResearchSnapshot> {
  try { return await getReplaySnapshot(); } catch (error) {
    const cached = await fromCache(() => readCachedResearchSnapshot("replay:latest", "ops.replay.v2"));
    if (cached) return cached;
    throw error;
  }
}

async function learningWithOfflineCache(): Promise<ResearchSnapshot> {
  try { return await getLearningResearchSnapshot(); } catch (error) {
    const cached = await fromCache(() => readCachedResearchSnapshot("learning:summary", "research.snapshot.v1"));
    if (cached) return cached;
    throw error;
  }
}

export function ResearchPage() {
  const [query, setQuery] = useState("");
  const [selectedReplayId, setSelectedReplayId] = useState<string | undefined>();
  const [windowBarsBefore, setWindowBarsBefore] = useState<number>(24);
  const [windowBarsAfter, setWindowBarsAfter] = useState<number>(24);
  const online = useNetworkStatus();
  const queryClient = useQueryClient();
  const bars = useQuery({ queryKey: ["research", "bars"], queryFn: marketWithOfflineCache, staleTime: 60_000, retry: false, refetchOnReconnect: true });
  const factors = useQuery({ queryKey: ["research", "factors"], queryFn: factorWithOfflineCache, staleTime: 60_000, retry: false, refetchOnReconnect: true });
  const replay = useQuery({ queryKey: ["research", "replay"], queryFn: replayWithOfflineCache, staleTime: 60_000, retry: false, refetchOnReconnect: true });
  const decisionTrace = useQuery({ queryKey: ["research", "decision-trace", 30, 60], queryFn: () => getReplayDecisionTrace(30, 60), staleTime: 60_000, retry: false, refetchOnReconnect: true });
  const learning = useQuery({ queryKey: ["research", "learning"], queryFn: learningWithOfflineCache, staleTime: 60_000, retry: false, refetchOnReconnect: true });
  const replayMutation = useMutation({ mutationFn: () => runReplay(selectedReplayId, windowBarsBefore, windowBarsAfter), onSuccess: (result) => { queryClient.setQueryData(["research", "replay"], result); } });

  useEffect(() => {
    if ((bars.data?.fact.state === "known" || bars.data?.fact.state === "stale") && bars.data.fact.observed_at) void putResearchCache("market:XAUUSD+:M15", "market.bars.v1", { bars: bars.data.bars, symbol: bars.data.symbol, timeframe: bars.data.timeframe }, bars.data.fact.source, bars.data.fact.observed_at, new Date(Date.now() + 30 * 60_000).toISOString()).catch(() => undefined);
  }, [bars.data]);
  useEffect(() => {
    if ((factors.data?.fact.state === "known" || factors.data?.fact.state === "stale") && factors.data.fact.observed_at) void putResearchCache("factor:catalog:latest", "factor.catalog.v4", factors.data, factors.data.fact.source, factors.data.fact.observed_at, new Date(Date.now() + 60 * 60_000).toISOString()).catch(() => undefined);
  }, [factors.data]);
  useEffect(() => {
    if ((replay.data?.fact.state === "known" || replay.data?.fact.state === "stale") && replay.data.fact.observed_at) void putResearchCache("replay:latest", "ops.replay.v2", replay.data, replay.data.fact.source, replay.data.fact.observed_at, new Date(Date.now() + 60 * 60_000).toISOString()).catch(() => undefined);
  }, [replay.data]);
  useEffect(() => {
    if ((learning.data?.fact.state === "known" || learning.data?.fact.state === "stale") && learning.data.fact.observed_at) void putResearchCache("learning:summary", "research.snapshot.v1", learning.data, learning.data.fact.source, learning.data.fact.observed_at, new Date(Date.now() + 60 * 60_000).toISOString()).catch(() => undefined);
  }, [learning.data]);

  const factorRows = useMemo(() => factors.data?.rows.filter((row) => `${row.title} ${row.id}`.toLowerCase().includes(query.toLowerCase())) ?? [], [factors.data?.rows, query]);
  const marketFact = queryFact(bars.data?.fact, bars.error, "market.bars.v1", "bars_not_loaded");
  const replayFact = queryFact(replay.data?.fact, replay.error, "ops.replay-latest.v2", "replay_not_loaded");
  const factorFact = queryFact(factors.data?.fact, factors.error, "factor.catalog.v4", "factor_catalog_not_loaded");
  const learningFact = queryFact(learning.data?.fact, learning.error, "learning.summary.v2", "learning_not_loaded");
  const decisionFact = queryFact(decisionTrace.data?.fact, decisionTrace.error, "ops.replay-bar-decisions.v2", "decision_trace_not_loaded");
  const decisionRows = decisionTrace.data?.items ?? [];
  const positionRows = useMemo(() => decisionRows.filter((item) => {
    const entryTs = entryTimestamp(item);
    const exitTs = exitTimestamp(item);
    return item.eventType === "open" && Boolean(item.positionId && item.decisionId && item.symbol && item.timeframe && entryTs > 0 && exitTs > entryTs);
  }).slice(0, 24), [decisionRows]);

  useEffect(() => {
    if (!positionRows.length) {
      setSelectedReplayId(undefined);
      return;
    }
    if (!selectedReplayId || !positionRows.some((item) => item.decisionId === selectedReplayId)) setSelectedReplayId(positionRows[0].decisionId ?? undefined);
  }, [positionRows, selectedReplayId]);

  const selectedPosition = positionRows.find((item) => item.decisionId === selectedReplayId);
  const selectedEntryTs = entryTimestamp(selectedPosition);
  const selectedExitTs = exitTimestamp(selectedPosition);
  const selectedTimeframe = selectedPosition?.timeframe?.toUpperCase() ?? "";
  const selectedTimeframeSec = timeframeSeconds(selectedTimeframe);
  const selectedHoldingSeconds = selectedExitTs > selectedEntryTs ? selectedExitTs - selectedEntryTs : 0;
  const selectedHoldingBars = selectedTimeframeSec && selectedHoldingSeconds > 0 ? Math.max(1, Math.ceil(selectedHoldingSeconds / selectedTimeframeSec)) : 1;
  const selectedWindowLimit = Math.min(5_000, windowBarsBefore + selectedHoldingBars + windowBarsAfter + 8);
  const selectedWindow = useQuery({
    queryKey: ["research", "selected-bar-window", selectedPosition?.decisionId, selectedEntryTs, selectedExitTs, selectedPosition?.symbol, selectedTimeframe, windowBarsBefore, windowBarsAfter],
    enabled: Boolean(selectedPosition && selectedPosition.symbol && selectedEntryTs > 0 && selectedExitTs > selectedEntryTs && selectedTimeframeSec),
    queryFn: () => {
      if (!selectedPosition?.symbol || selectedEntryTs <= 0 || selectedExitTs <= selectedEntryTs || !selectedTimeframeSec) throw new Error("selected_position_window_unavailable");
      return getMarketBars(selectedPosition.symbol, selectedTimeframe, selectedWindowLimit, {
        fromTs: selectedEntryTs - selectedTimeframeSec * windowBarsBefore,
        toTs: selectedExitTs + selectedTimeframeSec * windowBarsAfter,
      });
    },
    staleTime: 60_000,
    retry: false,
    refetchOnReconnect: true,
  });

  const selectedWindowFact = queryFact(selectedWindow.data?.fact, selectedWindow.error, "market.bars.v1", selectedTimeframeSec ? "selected_window_not_loaded" : "selected_timeframe_unknown");
  const marketFactForChart = selectedPosition ? selectedWindowFact : marketFact;
  const chartBars = selectedPosition ? selectedWindow.data?.bars ?? [] : bars.data?.bars.slice(-96) ?? [];
  const chartAllowed = Boolean((selectedPosition ? selectedWindow.data : bars.data) && (marketFactForChart.state === "known" || marketFactForChart.state === "stale"));
  const chartLoading = Boolean(selectedPosition && selectedWindow.isPending);
  const barCount = readableFact(marketFact) ? String(bars.data?.bars.length ?? 0) : "—";
  const factorCount = readableFact(factorFact) ? String(factors.data?.rows.length ?? 0) : "—";
  const replayCount = readableFact(replayFact) ? String(positionRows.length) : "—";
  const learningCount = readableFact(learningFact) ? String(learning.data?.rows.length ?? 0) : "—";
  const decisionCount = readableFact(decisionFact) ? String(decisionRows.length) : "—";

  return <div className="workspace-page research-page"><WorkspaceTitle kicker="03 / 证据画布" title="研究实验室" description="宽证据画布：行情、回放、因子和学习材料只读消费服务器事实，不直接应用到 runtime、治理或交易。" fact={marketFact} /><div className="workspace-toolbar"><span><FlaskConical size={14} />研究 / 只读</span><span>网络 / {online ? "在线" : "离线 · 仅缓存"}</span><span>缓存 / 行情 · 回放 · 因子 · 研究</span></div><div className="reference-fact-strip research-summary-strip">
    <div className="reference-fact-card"><span>K 线窗口</span><strong>{barCount}</strong><small><FactBadge compact fact={marketFact} /></small></div>
    <div className="reference-fact-card"><span>因子目录</span><strong>{factorCount}</strong><small><FactBadge compact fact={factorFact} /></small></div>
    <div className="reference-fact-card"><span>可回放仓位</span><strong>{replayCount}</strong><small><FactBadge compact fact={replayFact} /></small></div>
    <div className="reference-fact-card"><span>学习证据</span><strong>{learningCount}</strong><small><FactBadge compact fact={learningFact} /></small></div>
    <div className="reference-fact-card"><span>决策追踪</span><strong>{decisionCount}</strong><small><FactBadge compact fact={decisionFact} /></small></div>
  </div><div className="workspace-grid research-grid">
    <Panel title="行情画布" eyebrow="/api/market/bars" className="research-market-panel"><div className="research-canvas-head"><FactBadge fact={marketFactForChart} /><strong>{selectedPosition?.symbol ?? bars.data?.symbol ?? "XAUUSD+"} · {selectedPosition?.timeframe ?? bars.data?.timeframe ?? "M15"}</strong><span>{chartBars.length || "—"} 根 K 线</span></div>{selectedPosition && <div className="chart-focus-summary"><div><Crosshair size={14} /><strong>仓位 {selectedPosition.positionId}</strong><span>{directionLabel(selectedPosition.direction)} · {outcomeLabel(selectedPosition)}</span></div><div className="chart-focus-meta"><span>入场 {formatTimestamp(selectedEntryTs, "时间未知")}</span><span>出场 {formatTimestamp(selectedExitTs, "时间未知")}</span><span>{holdingDurationLabel(selectedHoldingSeconds)}</span><span>前置 {windowBarsBefore} 根</span><span>后置 {windowBarsAfter} 根</span></div></div>}<MarketChart bars={chartAllowed ? chartBars : []} markers={selectedPosition && selectedEntryTs > 0 && selectedExitTs > selectedEntryTs ? [{ t: selectedEntryTs, label: "入场", tone: "entry" }, { t: selectedExitTs, label: "出场", tone: "exit" }] : undefined} emptyLabel={chartLoading ? "正在加载入场至出场及前后 K 线…" : selectedPosition ? "该仓位行情窗口不可用；未显示猜测值" : bars.error ? "行情读取失败；未显示猜测值" : undefined} />{selectedPosition && <ChartSystemView item={selectedPosition} />}<p className="chart-note">{selectedPosition ? "左侧是入场前置，中央是持仓区间，右侧是出场后置；竖线为服务端入场/出场事实。" : "选择右侧已平仓历史仓位后，画布会显示入场至出场及前后置 K 线。"}</p><SourceLine fact={marketFactForChart} /></Panel>
    <Panel title="回放时间线" eyebrow="/api/ops/replay/*" className="replay-panel"><div className="panel-toolbar"><FactBadge fact={replayFact} /><button type="button" className="icon-action" onClick={() => replayMutation.mutate()} disabled={replayMutation.isPending || !online}><Play size={13} />{!online ? "离线只读" : replayMutation.isPending ? "提交中" : selectedPosition ? "回放选中仓位" : "触发服务端回放"}</button></div><div className="replay-window-controls"><span><Clock3 size={13} />前后置观察窗口</span><label>入场前置 <select value={windowBarsBefore} onChange={(event) => setWindowBarsBefore(Number(event.target.value))}>{WINDOW_BAR_OPTIONS.map((value) => <option key={`before-${value}`} value={value}>{value} 根</option>)}</select></label><label>出场后置 <select value={windowBarsAfter} onChange={(event) => setWindowBarsAfter(Number(event.target.value))}>{WINDOW_BAR_OPTIONS.map((value) => <option key={`after-${value}`} value={value}>{value} 根</option>)}</select></label></div><div className="replay-timeline-heading"><strong>历史仓位</strong><span>{positionRows.length} 条可选已平仓 · 选中后联动左侧行情</span></div><ReplayPositionTimeline items={positionRows} selectedDecisionId={selectedReplayId} onSelect={setSelectedReplayId} /><EvidenceLink label="回放引用" id={replay.data?.referenceId ?? null} />{replayMutation.error && <p className="action-error">{replayMutation.error instanceof Error ? replayMutation.error.message : "回放失败"}</p>}</Panel>
    <Panel title="因子目录" eyebrow="/api/v4/catalog" className="factor-panel"><label className="research-search"><Search size={14} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="筛选因子 ID / 名称" /></label><div className="panel-toolbar"><FactBadge fact={factorFact} /><span>{factorRows.length} 条可见 / {factors.data?.rows.length ?? "—"} 条总计</span></div><ResearchRows rows={factorRows} variant="factor" emptyMessage={factors.error ? "因子目录读取失败；未显示猜测值。" : undefined} /><EvidenceLink label="因子引用" id={factors.data?.referenceId ?? null} /></Panel>
    <Panel title="学习证据" eyebrow="/api/learning/*" className="learning-evidence-panel"><FactBadge fact={learningFact} /><ResearchRows rows={learning.data?.rows ?? []} variant="learning" emptyMessage={learning.error ? "学习证据读取失败；未显示猜测值。" : undefined} /></Panel>
    <Panel title="决策追踪 / PIT" eyebrow="/api/ops/replay/bar-decisions" className="decision-trace-panel"><div className="panel-toolbar"><FactBadge fact={decisionFact} /><span>decision_id → 回放窗口 → 证据</span></div><DecisionTraceRows items={decisionRows} emptyMessage={decisionTrace.error ? "决策追踪读取失败；未显示猜测值。" : undefined} /></Panel>
  </div></div>;
}

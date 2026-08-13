import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, FlaskConical, Play, Search } from "lucide-react";
import type { FactEnvelope } from "@/api/fact";
import { formatObservedTime } from "@/api/time";
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

function ResearchRows({ rows, emptyMessage = "证据结果为空；这不是成功或失败的替代值。" }: { rows: ResearchRow[]; emptyMessage?: string }) {
  if (!rows.length) return <div className="empty-confirmed">{emptyMessage}</div>;
  return <div className="research-row-list">{rows.slice(0, 12).map((row) => <div className="research-row" key={row.id}>{row.title !== row.id && <span className="research-row-id">{row.id}</span>}<strong>{row.title}</strong><span>{uiStatus(row.state)}</span>{row.reasonCode && <code>{row.reasonCode}</code>}{row.detail && <small>{row.detail}</small>}<time>{formatObservedTime(row.observedAt)}</time></div>)}</div>;
}

function DecisionTraceRows({ items, emptyMessage = "证据结果为空；这不是成功或失败的替代值。" }: { items: DecisionTrace[]; emptyMessage?: string }) {
  if (!items.length) return <div className="empty-confirmed">{emptyMessage}</div>;
  return <div className="research-row-list">{items.slice(0, 12).map((item) => <div className="research-row" key={item.traceId}>
    <span className="research-row-id">{item.decisionId ?? item.traceId}</span>
    <strong>{[item.eventType, item.symbol, item.timeframe].filter(Boolean).join(" · ") || "决策记录"}</strong>
    <span>{item.positionId ? `仓位 ${item.positionId}` : "未关联仓位"}</span>
    {(item.actionReason || item.reasonCode || item.outcomeResult) && <code>{item.actionReason ?? item.reasonCode ?? item.outcomeResult}</code>}
    {(item.outcomeStatus || item.outcomeLabel || item.learningStatus) && <small>{[item.outcomeStatus, item.outcomeLabel, item.learningStatus].filter(Boolean).map((value) => uiStatus(value)).join(" · ")}</small>}
    <time>{formatObservedTime(item.observedAt)}</time>
  </div>)}</div>;
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
  const [replayId, setReplayId] = useState<string | undefined>();
  const online = useNetworkStatus();
  const queryClient = useQueryClient();
  const bars = useQuery({ queryKey: ["research", "bars"], queryFn: marketWithOfflineCache, staleTime: 60_000, retry: false, refetchOnReconnect: true });
  const factors = useQuery({ queryKey: ["research", "factors"], queryFn: factorWithOfflineCache, staleTime: 60_000, retry: false, refetchOnReconnect: true });
  const replay = useQuery({ queryKey: ["research", "replay"], queryFn: replayWithOfflineCache, staleTime: 60_000, retry: false, refetchOnReconnect: true });
  const decisionTrace = useQuery({ queryKey: ["research", "decision-trace"], queryFn: getReplayDecisionTrace, staleTime: 60_000, retry: false, refetchOnReconnect: true });
  const learning = useQuery({ queryKey: ["research", "learning"], queryFn: learningWithOfflineCache, staleTime: 60_000, retry: false, refetchOnReconnect: true });
  const replayMutation = useMutation({ mutationFn: () => runReplay(replayId), onSuccess: (result) => { queryClient.setQueryData(["research", "replay"], result); } });

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
  const chartAllowed = bars.data && (marketFact.state === "known" || marketFact.state === "stale");
  const decisionRows = decisionTrace.data?.items ?? [];

  return <div className="workspace-page research-page"><WorkspaceTitle kicker="03 / 证据画布" title="研究实验室" description="宽证据画布：行情、回放、因子和学习材料只读消费服务器事实，不直接应用到 runtime、治理或交易。" fact={marketFact} /><div className="workspace-toolbar"><span><FlaskConical size={14} />研究 / 只读</span><span>网络 / {online ? "在线" : "离线 · 仅缓存"}</span><span>缓存 / 行情 · 回放 · 因子 · 研究</span></div><div className="workspace-grid research-grid">
    <Panel title="行情画布" eyebrow="/api/market/bars" className="research-market-panel"><div className="research-canvas-head"><FactBadge fact={marketFact} /><strong>{bars.data?.symbol ?? "XAUUSD+"} · {bars.data?.timeframe ?? "M15"}</strong><span>{bars.data?.bars.length ?? "—"} 根 K 线</span></div><MarketChart bars={chartAllowed ? bars.data.bars.slice(-96) : []} emptyLabel={bars.error ? "行情读取失败；未显示猜测值" : undefined} /><SourceLine fact={marketFact} /></Panel>
    <Panel title="回放时间线" eyebrow="/api/ops/replay/*" className="replay-panel"><div className="panel-toolbar"><FactBadge fact={replayFact} /><button type="button" className="icon-action" onClick={() => replayMutation.mutate()} disabled={replayMutation.isPending || !online}><Play size={13} />{!online ? "离线只读" : replayMutation.isPending ? "提交中" : "触发服务端回放"}</button></div><ResearchRows rows={replay.data?.rows ?? []} emptyMessage={replay.error ? "回放读取失败；未显示猜测值。" : undefined} /><EvidenceLink label="回放引用" id={replay.data?.referenceId ?? null} />{replayMutation.error && <p className="action-error">{replayMutation.error instanceof Error ? replayMutation.error.message : "回放失败"}</p>}</Panel>
    <Panel title="因子目录" eyebrow="/api/v4/catalog" className="factor-panel"><label className="research-search"><Search size={14} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="筛选因子 ID / 名称" /></label><div className="panel-toolbar"><FactBadge fact={factorFact} /><span>{factorRows.length} 条可见 / {factors.data?.rows.length ?? "—"} 条总计</span></div><ResearchRows rows={factorRows} emptyMessage={factors.error ? "因子目录读取失败；未显示猜测值。" : undefined} /><EvidenceLink label="因子引用" id={factors.data?.referenceId ?? null} /></Panel>
    <Panel title="学习证据" eyebrow="/api/learning/*" className="learning-evidence-panel"><FactBadge fact={learningFact} /><ResearchRows rows={learning.data?.rows ?? []} emptyMessage={learning.error ? "学习证据读取失败；未显示猜测值。" : undefined} /></Panel>
    <Panel title="决策追踪 / PIT" eyebrow="/api/ops/replay/bar-decisions" className="decision-trace-panel"><div className="panel-toolbar"><FactBadge fact={decisionFact} /><span>decision_id → 回放窗口 → 证据</span></div><DecisionTraceRows items={decisionRows} emptyMessage={decisionTrace.error ? "决策追踪读取失败；未显示猜测值。" : undefined} /></Panel>
  </div></div>;
}

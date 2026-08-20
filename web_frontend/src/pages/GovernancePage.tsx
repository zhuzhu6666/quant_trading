import { useQuery } from "@tanstack/react-query";
import { ClipboardCheck, GitCommitHorizontal, ShieldCheck } from "lucide-react";
import type { FactEnvelope } from "@/api/fact";
import { formatObservedTime } from "@/api/time";
import { getGovernanceCandidates, getGovernanceProposals, getGovernanceReviews, getReleaseEvidence } from "@/api/workbench";
import { FactBadge, Panel, SourceLine } from "@/design-system/primitives";
import type { GovernanceRecord } from "@/types/contracts";
import { WorkspaceTitle } from "@/workspaces/WorkspaceBits";
import { uiStatus } from "@/i18n/zh-CN";

const unavailableFact = (contract: string, reasonCode: string, state: FactEnvelope["state"] = "unknown"): FactEnvelope => ({ envelope: "fact.v1", contract, state, source: "none", observed_at: null, generated_at: null, stale_after_sec: 0, reason_code: reasonCode, components: {} });
const queryFact = (fact: FactEnvelope | undefined, error: unknown, contract: string, reasonCode: string): FactEnvelope => fact ?? unavailableFact(contract, error ? `${reasonCode}_request_failed` : reasonCode, error ? "error" : "unknown");

type GovernanceItem = GovernanceRecord;

function GovernanceRows({ items, emptyMessage = "暂无已确认记录；空列表不等于可批准。" }: { items: GovernanceItem[]; emptyMessage?: string }) {
  if (!items.length) return <div className="empty-confirmed">{emptyMessage}</div>;
  return <div className="governance-list">{items.slice(0, 14).map((item) => <div className="governance-row" key={item.id}>
    <div className="governance-row-head"><span className="governance-id" title={item.id}>{item.id}</span><strong title={item.status}>{uiStatus(item.status)}</strong><time title="该条持久化记录的时间">记录：{formatObservedTime(item.observedAt)}</time></div>
    {(item.action || item.target || item.source || item.stage) && <div className="governance-row-context">
      {item.action && <span><b>动作</b>{item.action}</span>}
      {item.target && <span><b>目标</b>{item.target}</span>}
      {item.source && <span><b>来源</b>{item.source}</span>}
      {item.stage && <span><b>阶段</b>{item.stage}</span>}
    </div>}
    {(item.reasonCode || item.authorityState) && <div className="governance-row-reason">{item.reasonCode && <code>{item.reasonCode}</code>}{item.authorityState && <span>权力状态：{uiStatus(item.authorityState)}</span>}</div>}
    {(item.durableId || item.auditId || item.commitStatus) && <small>{[item.durableId && `持久 ID：${item.durableId}`, item.auditId && `审计 ID：${item.auditId}`, item.commitStatus && `提交：${uiStatus(item.commitStatus)}`].filter(Boolean).join(" · ")}</small>}
  </div>)}</div>;
}

export function GovernancePage() {
  const candidates = useQuery({ queryKey: ["governance", "candidates"], queryFn: getGovernanceCandidates, staleTime: 30_000, retry: false });
  const reviews = useQuery({ queryKey: ["governance", "reviews"], queryFn: getGovernanceReviews, staleTime: 30_000, retry: false });
  const proposals = useQuery({ queryKey: ["governance", "proposals"], queryFn: getGovernanceProposals, staleTime: 30_000, retry: false });
  const release = useQuery({ queryKey: ["governance", "release"], queryFn: getReleaseEvidence, staleTime: 30_000, retry: false });
  const candidatesFact = queryFact(candidates.data?.fact, candidates.error, "ops.v16-governance-candidates.v2", "governance_candidates_not_loaded");
  const reviewsFact = queryFact(reviews.data?.fact, reviews.error, "ops.v16-governance-candidate-reviews.v2", "governance_reviews_not_loaded");
  const proposalsFact = queryFact(proposals.data?.fact, proposals.error, "ops.autonomy-proposals.v2", "governance_proposals_not_loaded");
  const releaseFact = queryFact(release.data?.fact, release.error, "ops.release-latest.v2", "governance_release_not_loaded");
  const confirmedCount = (fact: FactEnvelope, count: number | undefined): string => fact.state === "known" || fact.state === "stale" ? String(count ?? 0) : "—";
  const candidateCount = confirmedCount(candidatesFact, candidates.data?.items.length);
  const reviewCount = confirmedCount(reviewsFact, reviews.data?.items.length);
  const proposalCount = confirmedCount(proposalsFact, proposals.data?.items.length);
  const releaseCount = confirmedCount(releaseFact, release.data?.items.length);

  return <div className="workspace-page governance-page"><WorkspaceTitle kicker="04 / 控制平面" title="治理中心" description="候选、审查、提案、mutation、release 和审计轨迹。前端不批准、不应用，不绕过 Coordinator、V16 或 RiskPolicy。" fact={candidatesFact} /><div className="workspace-toolbar"><span><ShieldCheck size={14} />治理 / 服务端门控</span><span>必须有持久 ID + 审计 ID</span><span>mutation / 回读提交状态</span></div><div className="reference-fact-strip governance-summary-strip">
    <div className="reference-fact-card"><span>候选队列</span><strong>{candidateCount}</strong><small><FactBadge compact fact={candidatesFact} /></small></div>
    <div className="reference-fact-card"><span>候选审查</span><strong>{reviewCount}</strong><small><FactBadge compact fact={reviewsFact} /></small></div>
    <div className="reference-fact-card"><span>提案登记</span><strong>{proposalCount}</strong><small><FactBadge compact fact={proposalsFact} /></small></div>
    <div className="reference-fact-card"><span>发布证据</span><strong>{releaseCount}</strong><small><FactBadge compact fact={releaseFact} /></small></div>
    <div className="reference-fact-card reference-fact-card-note"><span>授权边界</span><strong>服务端门控</strong><small>空列表不等于允许</small></div>
  </div><div className="workspace-grid governance-grid">
    <Panel title="候选队列" eyebrow="V16 治理候选" className="governance-candidates"><div className="panel-toolbar"><FactBadge fact={candidatesFact} /><span>{candidates.data?.items.length ?? "—"} 条记录</span><span className="control-boundary"><ClipboardCheck size={13} />进入桥接前必须审查</span></div><GovernanceRows items={candidates.data?.items ?? []} emptyMessage={candidates.error ? "候选队列读取失败；未显示猜测值。" : candidatesFact.state === "known" ? "当前没有可审候选；后端查询已完成，历史候选均已进入终态。" : "候选数据尚未确认；未显示猜测值。"} /></Panel>
    <Panel title="候选审查" eyebrow="候选审查" className="governance-reviews"><div className="panel-toolbar"><FactBadge fact={reviewsFact} /><span>仅服务端审查</span></div><GovernanceRows items={reviews.data?.items ?? []} emptyMessage={reviews.error ? "候选审查读取失败；未显示猜测值。" : undefined} /></Panel>
    <Panel title="提案登记" eyebrow="/api/ops/autonomy/proposals" className="governance-proposals"><div className="panel-toolbar"><FactBadge fact={proposalsFact} /><span>只读投影</span></div><GovernanceRows items={proposals.data?.items ?? []} emptyMessage={proposals.error ? "提案登记读取失败；未显示猜测值。" : undefined} /></Panel>
    <Panel title="发布证据" eyebrow="/api/ops/release/*" className="governance-release"><div className="panel-toolbar"><FactBadge fact={releaseFact} /><span><GitCommitHorizontal size={13} /> 提交 / 状态</span></div><div className="governance-release-note">这里显示后端 release ledger 的最近一条持久化记录；记录很旧只说明近期没有新的发布，不代表查询时间是旧的，也不会伪造新的发布证据。</div><GovernanceRows items={release.data?.items ?? []} emptyMessage={release.error ? "发布证据读取失败；未显示猜测值。" : releaseFact.state === "known" ? "当前没有发布记录。" : "发布证据尚未确认；未显示猜测值。"} /><SourceLine fact={releaseFact} /></Panel>
  </div></div>;
}

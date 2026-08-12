import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2,
  ClipboardList,
  FileSearch2,
  Play,
  RefreshCw,
  Rocket,
  ShieldAlert,
  SlidersHorizontal,
} from "lucide-react";
import {
  enforceAutonomyScope,
  getAutonomyScopeEnforcementLatest,
  getIncidentControl,
  getIncidentPlaybookLatest,
  getReleaseApprovals,
  getReleaseLatest,
  getReplayBarDecisions,
  getReplayLatest,
  runIncidentPlaybook,
  runReplayBarEvidence,
  setIncidentControl,
  startReleaseRun,
} from "@/api/client";
import { ActionButton } from "@/components/ActionButton";
import { MetricCard } from "@/components/Card";
import { FactBoundary } from "@/components/FactBoundary";
import { Field, PageHeader, SectionHead, toneFromStatus, type Tone } from "@/components/DashboardBits";
import { JsonBlock } from "@/components/JsonBlock";
import { StatusPill } from "@/components/StatusPill";
import { factBoundTone, factHasDisplayValue, factIsKnown, readFact } from "@/api/fact";
import { asRecord, pick, pickArray, pickBoolean, pickNumber, pickString } from "@/lib/compat";
import { translateDisplayValue } from "@/lib/display";
import { formatDecimal, formatTime } from "@/lib/format";

type EvidenceTab = "replay" | "incident" | "release";

const tabs: Array<{ key: EvidenceTab; label: string; icon: typeof Play }> = [
  { key: "replay", label: "回放证据", icon: Play },
  { key: "incident", label: "事故控制", icon: ShieldAlert },
  { key: "release", label: "发布审计", icon: Rocket },
];

function outcomeTone(result: string, status: string): Tone {
  if (result === "profit") return "ok";
  if (result === "loss") return "bad";
  if (status === "open" || status === "awaiting_outcome") return "warn";
  return "mute";
}

function learningTone(status: string): Tone {
  if (status === "learning_sample_ready") return "ok";
  if (status === "awaiting_outcome" || status === "awaiting_learning_sample" || status === "learning_sample_observe") return "warn";
  return "mute";
}

function EvidenceRows({ items, empty }: { items: unknown[]; empty: string }) {
  if (!items.length) return <div className="empty-state-small">{empty}</div>;
  return (
    <div className="evidence-list">
      {items.slice(0, 12).map((raw, index) => {
        const item = asRecord(raw);
        const title = pickString(item, ["component", "name", "decision_id", "event_type", "status", "id"], `#${index + 1}`);
        const detail = pickString(item, ["reason", "message", "summary", "decision", "route", "created_at"], "");
        const status = pickString(item, ["status", "state", "result", "decision"], "");
        return (
          <div className="evidence-row" key={`${title}-${index}`}>
            <div>
              <strong>{translateDisplayValue(title)}</strong>
              {detail ? <span>{translateDisplayValue(detail)}</span> : null}
            </div>
            {status ? <StatusPill status={translateDisplayValue(status)} tone={toneFromStatus(status)} /> : null}
          </div>
        );
      })}
    </div>
  );
}

function KlineWindowPreview({ preview, pending }: { preview: unknown; pending: boolean }) {
  const windows = pickArray(preview, ["windows"]).map(asRecord);
  if (!windows.length) {
    return <div className="replay-kline-empty">{pending ? "正在生成 K 线窗口…" : "生成回放后显示前 40 根和后 24 根 K 线"}</div>;
  }

  const window = windows[0];
  const bars = pickArray(window, ["bars"]).map(asRecord);
  if (!bars.length) return <div className="replay-kline-empty">当前窗口没有 OHLC 数据</div>;

  const width = 960;
  const height = 260;
  const pad = 22;
  const highs = bars.map((bar) => pickNumber(bar, ["high"], 0)).filter(Number.isFinite);
  const lows = bars.map((bar) => pickNumber(bar, ["low"], 0)).filter(Number.isFinite);
  const maxPrice = Math.max(...highs);
  const minPrice = Math.min(...lows);
  const priceSpan = Math.max(0.000001, maxPrice - minPrice);
  const xStep = bars.length > 1 ? (width - pad * 2) / (bars.length - 1) : width - pad * 2;
  const candleWidth = Math.max(3, Math.min(10, xStep * 0.58));
  const yFor = (price: number) => pad + ((maxPrice - price) / priceSpan) * (height - pad * 2);
  const decisionTs = pickNumber(window, ["decision_ts"], 0);
  const decisionIndex = bars.reduce((best, bar, index) => {
    const currentDistance = Math.abs(pickNumber(bar, ["time"], 0) - decisionTs);
    const bestDistance = Math.abs(pickNumber(bars[best], ["time"], 0) - decisionTs);
    return currentDistance < bestDistance ? index : best;
  }, 0);
  const decisionX = pad + decisionIndex * xStep;
  const afterBars = bars.filter((bar) => pickNumber(bar, ["time"], 0) > decisionTs);
  const decisionClose = pickNumber(bars[decisionIndex], ["close"], 0);
  const futureHigh = afterBars.length ? Math.max(...afterBars.map((bar) => pickNumber(bar, ["high"], decisionClose))) : decisionClose;
  const futureLow = afterBars.length ? Math.min(...afterBars.map((bar) => pickNumber(bar, ["low"], decisionClose))) : decisionClose;
  const finalClose = afterBars.length ? pickNumber(afterBars[afterBars.length - 1], ["close"], decisionClose) : decisionClose;

  return (
    <div className="replay-kline">
      <div className="replay-kline-head">
        <div>
          <strong>{pickString(window, ["symbol"], "XAUUSD+")} · {pickString(window, ["timeframe"], "M5")}</strong>
          <span>{pending ? "正在更新，暂时保留上一次窗口" : `${bars.length} 根 K 线`}</span>
        </div>
        <span className="replay-kline-legend"><i />决策点 <b />决策后</span>
      </div>
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="决策前后 K 线回放窗口">
        <line x1={pad} x2={width - pad} y1={height - pad} y2={height - pad} className="replay-kline-axis" />
        {bars.map((bar, index) => {
          const open = pickNumber(bar, ["open"], 0);
          const high = pickNumber(bar, ["high"], 0);
          const low = pickNumber(bar, ["low"], 0);
          const close = pickNumber(bar, ["close"], 0);
          const x = pad + index * xStep;
          const up = close >= open;
          const bodyTop = yFor(Math.max(open, close));
          const bodyHeight = Math.max(2, Math.abs(yFor(open) - yFor(close)));
          return (
            <g key={`${pickNumber(bar, ["time"], index)}-${index}`} className={up ? "replay-candle-up" : "replay-candle-down"}>
              <line x1={x} x2={x} y1={yFor(high)} y2={yFor(low)} />
              <rect x={x - candleWidth / 2} y={bodyTop} width={candleWidth} height={bodyHeight} rx="1" />
            </g>
          );
        })}
        {decisionTs ? (
          <>
            <rect x={decisionX} y={pad} width={Math.max(0, width - pad - decisionX)} height={height - pad * 2} className="replay-kline-future" />
            <line x1={decisionX} x2={decisionX} y1={pad} y2={height - pad} className="replay-kline-decision" />
          </>
        ) : null}
      </svg>
      <div className="replay-kline-meta">
        <span>最高 <strong>{formatDecimal(maxPrice, 2)}</strong></span>
        <span>最低 <strong>{formatDecimal(minPrice, 2)}</strong></span>
        <span>决策时间 <strong>{formatTime(decisionTs)}</strong></span>
        <span>后续 <strong>{afterBars.length} 根</strong></span>
        <span>上冲 <strong>{formatDecimal(futureHigh - decisionClose, 2)}</strong></span>
        <span>下探 <strong>{formatDecimal(futureLow - decisionClose, 2)}</strong></span>
        <span>最终变化 <strong>{formatDecimal(finalClose - decisionClose, 2)}</strong></span>
      </div>
    </div>
  );
}

export function EvidencePage() {
  const [activeTab, setActiveTab] = useState<EvidenceTab>("replay");
  const [decisionId, setDecisionId] = useState("latest_trade");
  const [replayPreviewData, setReplayPreviewData] = useState<unknown>(null);
  const queryClient = useQueryClient();

  const replayQuery = useQuery({
    queryKey: ["evidence", "replay-latest"],
    queryFn: getReplayLatest,
    enabled: activeTab === "replay",
    staleTime: 10_000,
  });
  const replayChoicesQuery = useQuery({
    queryKey: ["evidence", "replay-decisions"],
    queryFn: () => getReplayBarDecisions(30),
    enabled: activeTab === "replay",
    staleTime: 20_000,
  });
  const incidentQuery = useQuery({
    queryKey: ["evidence", "incident-control"],
    queryFn: getIncidentControl,
    enabled: activeTab === "incident",
    staleTime: 5_000,
  });
  const playbookQuery = useQuery({
    queryKey: ["evidence", "incident-playbook"],
    queryFn: getIncidentPlaybookLatest,
    enabled: activeTab === "incident",
    staleTime: 10_000,
  });
  const enforcementQuery = useQuery({
    queryKey: ["evidence", "scope-enforcement"],
    queryFn: getAutonomyScopeEnforcementLatest,
    enabled: activeTab === "incident",
    staleTime: 15_000,
  });
  const releaseQuery = useQuery({
    queryKey: ["evidence", "release-latest"],
    queryFn: getReleaseLatest,
    enabled: activeTab === "release",
    staleTime: 10_000,
  });

  const releaseRaw = asRecord(pick(releaseQuery.data, ["release"]));
  const releaseRunIdRaw = pickString(releaseRaw, ["run_id"], "");
  const releaseApprovalsQuery = useQuery({
    queryKey: ["evidence", "release-approvals", releaseRunIdRaw],
    queryFn: () => getReleaseApprovals(releaseRunIdRaw),
    enabled: activeTab === "release" && Boolean(releaseRunIdRaw),
    staleTime: 15_000,
  });

  const replayMutation = useMutation({
    mutationFn: () => runReplayBarEvidence(decisionId === "latest_trade" ? "" : decisionId),
    onSuccess: setReplayPreviewData,
  });
  const incidentModeMutation = useMutation({
    mutationFn: (mode: string) => setIncidentControl(mode, `web_evidence:${mode}`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["evidence", "incident-control"] }),
  });
  const playbookMutation = useMutation({
    mutationFn: () => runIncidentPlaybook("governance_failure", "medium"),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["evidence", "incident-playbook"] }),
  });
  const enforceMutation = useMutation({
    mutationFn: enforceAutonomyScope,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["evidence", "scope-enforcement"] });
      void queryClient.invalidateQueries({ queryKey: ["evidence", "incident-control"] });
    },
  });
  const releaseMutation = useMutation({
    mutationFn: startReleaseRun,
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["evidence", "release-latest"] }),
  });

  const replayFact = readFact(replayQuery.data, "ops.replay-latest.v2");
  const replayChoicesFact = readFact(replayChoicesQuery.data, "ops.replay-bar-decisions.v2");
  const replayPreviewFact = readFact(replayPreviewData, "ops.replay-bar-preview.v2");
  const incidentFact = readFact(incidentQuery.data, "ops.incident-control.v2");
  const playbookFact = readFact(playbookQuery.data, "ops.incident-playbook-latest.v2");
  const enforcementFact = readFact(enforcementQuery.data, "ops.autonomy-scope-enforcement-latest.v2");
  const releaseFact = readFact(releaseQuery.data, "ops.release-latest.v2");
  const approvalsFact = readFact(releaseApprovalsQuery.data, "ops.release-approval-trail.v2");

  const replayFailed = replayQuery.isError || replayQuery.isRefetchError;
  const replayChoicesFailed = replayChoicesQuery.isError || replayChoicesQuery.isRefetchError;
  const incidentFailed = incidentQuery.isError || incidentQuery.isRefetchError;
  const playbookFailed = playbookQuery.isError || playbookQuery.isRefetchError;
  const enforcementFailed = enforcementQuery.isError || enforcementQuery.isRefetchError;
  const releaseFailed = releaseQuery.isError || releaseQuery.isRefetchError;
  const approvalsFailed = releaseApprovalsQuery.isError || releaseApprovalsQuery.isRefetchError;

  const latestReplay = factHasDisplayValue(replayFact, replayFailed)
    ? asRecord(pick(replayQuery.data, ["replay", "latest_report", "report"]))
    : {};
  const previewReport = asRecord(pick(replayPreviewData, ["report"]));
  const hasReplayPreview = Object.keys(previewReport).length > 0;
  const replayReport = hasReplayPreview ? previewReport : latestReplay;
  const replayDisplayFact = hasReplayPreview ? replayPreviewFact : replayFact;
  const replayDisplayFailed = hasReplayPreview ? false : replayFailed;
  const replayChoices = factHasDisplayValue(replayChoicesFact, replayChoicesFailed)
    ? pickArray(replayChoicesQuery.data, ["items", "choices"])
    : [];
  const replayMetrics = asRecord(pick(replayReport, ["metric_summary"]));
  const barWindowPreview = pick(replayMetrics, ["bar_window_preview"]);
  const tradeOutcomePreview = asRecord(pick(replayMetrics, ["trade_outcome_learning_preview"]));
  const tradeOutcomeItem = asRecord(pickArray(tradeOutcomePreview, ["items"])[0]);
  const tradeOutcome = asRecord(pick(tradeOutcomeItem, ["outcome"]));
  const tradeLearning = asRecord(pick(tradeOutcomeItem, ["learning"]));
  const tradeResult = pickString(tradeOutcome, ["result"], "");
  const tradeStatus = pickString(tradeOutcome, ["status"], "");
  const tradeLearningStatus = pickString(tradeLearning, ["status"], "");
  const tradeDirection = translateDisplayValue(pickString(tradeOutcomeItem, ["direction_label", "direction"], ""));
  const tradePnl = pickNumber(tradeOutcome, ["pnl"], 0);
  const tradePnlText =
    tradeStatus === "no_trade" ? "未交易"
      : tradeStatus === "open" ? "未平仓"
        : tradeStatus === "missing" || !tradeResult ? "结果未知"
          : `${tradePnl >= 0 ? "+" : ""}${formatDecimal(tradePnl, 2)}`;
  const tradeReference = pickString(tradeOutcomeItem, ["position_id"], "") || pickString(tradeOutcomeItem, ["trade_id"], "");
  const closeReason = translateDisplayValue(pickString(tradeOutcome, ["close_reason"], ""));
  const tradeFactorDetail = [
    pickString(tradeOutcome, ["primary_factor"], "") ? `主因 ${pickString(tradeOutcome, ["primary_factor"], "")}` : "",
    pickString(tradeOutcome, ["worst_factor"], "") ? `拖累 ${pickString(tradeOutcome, ["worst_factor"], "")}` : "",
  ].filter(Boolean).join(" · ");
  const mismatchItems = pickArray(replayMetrics, ["bar_replay.mismatch_examples", "mismatch_examples"]);
  const replayGrade = pickString(replayReport, ["evidence_grade"], "");
  const replayError = pickString(replayReport, ["replay_error"], "");
  const metricEntries = useMemo(
    () => Object.entries(replayMetrics).filter(([, value]) => value && typeof value === "object").slice(0, 10),
    [replayMetrics],
  );

  const incident = factHasDisplayValue(incidentFact, incidentFailed) ? asRecord(pick(incidentQuery.data, ["incident_control"])) : {};
  const playbook = factHasDisplayValue(playbookFact, playbookFailed) ? asRecord(pick(playbookQuery.data, ["playbook"])) : {};
  const enforcement = factHasDisplayValue(enforcementFact, enforcementFailed) ? asRecord(pick(enforcementQuery.data, ["enforcement_event"])) : {};
  const release = factHasDisplayValue(releaseFact, releaseFailed) ? releaseRaw : {};
  const releaseRunId = pickString(release, ["run_id"], "");
  const incidentKnown = factIsKnown(incidentFact, incidentFailed);
  const incidentMode = pickString(incident, ["mode"], "");
  const approvals = factHasDisplayValue(approvalsFact, approvalsFailed)
    ? pickArray(releaseApprovalsQuery.data, ["events", "approvals", "approval_events"])
    : [];

  const refreshActive = () => {
    void queryClient.invalidateQueries({ queryKey: ["evidence"] });
  };

  return (
    <section className="dashboard evidence-dashboard">
      <PageHeader eyebrow="系统运维" title="运行证据" description="回放、事故控制和发布审计各归其位；进入分区或手动刷新时读取。">
        <button className="header-refresh" type="button" onClick={refreshActive}>
          <RefreshCw size={15} aria-hidden="true" />刷新当前分区
        </button>
      </PageHeader>

      <nav className="section-tabs" aria-label="运行证据分区">
        {tabs.map((tab) => {
          const Icon = tab.icon;
          return (
            <button
              key={tab.key}
              className={`section-tab ${activeTab === tab.key ? "section-tab-active" : ""}`}
              type="button"
              aria-current={activeTab === tab.key ? "page" : undefined}
              onClick={() => setActiveTab(tab.key)}
            >
              <Icon size={15} aria-hidden="true" />{tab.label}
            </button>
          );
        })}
      </nav>

      {activeTab === "replay" ? (
        <FactBoundary fact={replayDisplayFact} label="回放事实" requestFailed={replayDisplayFailed}>
        <div className="dashboard-grid">
          <MetricCard title="证据窗口" className="wide-panel">
            <div className="evidence-toolbar">
              <label>
                <span>复盘交易</span>
                <select value={decisionId} onChange={(event) => setDecisionId(event.target.value)}>
                  <option value="latest_trade">最近一笔交易</option>
                  {replayChoices.map((raw, index) => {
                    const item = asRecord(raw);
                    const value = pickString(item, ["decision_id"], String(index));
                    const direction = translateDisplayValue(pickString(item, ["direction_label", "direction"], ""));
                    const result = pickString(item, ["outcome_result", "outcome_status"], "");
                    const pnl = pickNumber(item, ["pnl"], 0);
                    const resultLabel = result ? `${translateDisplayValue(result)}${["profit", "loss", "flat"].includes(result) ? ` ${pnl >= 0 ? "+" : ""}${formatDecimal(pnl, 2)}` : ""}` : "";
                    const label = [
                      pickString(item, ["symbol"], ""),
                      direction,
                      resultLabel,
                      formatTime(pick(item, ["decision_ts", "created_at", "ts"])),
                    ].filter(Boolean).join(" · ");
                    return <option key={`${value}-${index}`} value={value}>{label || value}</option>;
                  })}
                </select>
              </label>
              <div className="action-wrap">
                <button
                  className="action-btn action-primary"
                  type="button"
                  disabled={replayMutation.isPending}
                  aria-busy={replayMutation.isPending || undefined}
                  onClick={() => replayMutation.mutate()}
                >
                  <Play size={16} aria-hidden="true" />
                  <span>{replayMutation.isPending ? "生成中…" : "生成回放"}</span>
                </button>
                {replayMutation.isError ? <div className="small error-text action-error" role="alert">回放请求失败</div> : null}
              </div>
            </div>
            {replayError ? <div className="error-text evidence-replay-error" role="alert">{replayError}</div> : null}
            <div className="evidence-summary">
              <Field label="证据等级" value={replayGrade} tone={factBoundTone(replayDisplayFact, replayGrade === "A" || replayGrade === "B" ? "ok" : "warn", replayDisplayFailed)} />
              <Field label="决策数量" value={formatDecimal(pickNumber(replayReport, ["decision_count"], 0), 0)} />
              <Field label="匹配数量" value={formatDecimal(pickNumber(replayReport, ["matched_live_count"], 0), 0)} />
              <Field label="不一致" value={formatDecimal(pickNumber(replayReport, ["mismatch_count"], 0), 0)} tone={factBoundTone(replayDisplayFact, pickNumber(replayReport, ["mismatch_count"], 0) ? "warn" : "ok", replayDisplayFailed)} />
              <Field label="更新时间" value={formatTime(pick(replayReport, ["created_at", "updated_at"]))} />
              <Field label="证据哈希" value={pickString(replayReport, ["artifact_hash"], "").slice(0, 16)} />
            </div>
          </MetricCard>

          <MetricCard title="K 线回放窗口" className="wide-panel">
            <KlineWindowPreview preview={barWindowPreview} pending={replayMutation.isPending} />
          </MetricCard>

          <MetricCard title="交易结果与学习" className="wide-panel">
            {Object.keys(tradeOutcomeItem).length ? (
              <>
                <div className={`trade-outcome-banner trade-outcome-${outcomeTone(tradeResult, tradeStatus)}`}>
                  <div>
                    <span>{pickString(tradeOutcomeItem, ["symbol"], "")} · 开仓方向</span>
                    <strong>{tradeDirection || "方向未知"}</strong>
                  </div>
                  <div>
                    <span>实际盈亏</span>
                    <strong>{tradePnlText}</strong>
                  </div>
                  <StatusPill
                    status={translateDisplayValue(tradeResult || tradeStatus)}
                    tone={factBoundTone(replayDisplayFact, outcomeTone(tradeResult, tradeStatus), replayDisplayFailed)}
                    fact={replayDisplayFact}
                    requestFailed={replayDisplayFailed}
                  />
                </div>
                <div className="evidence-summary">
                  <Field label="仓位/交易" value={tradeReference || pickString(tradeOutcomeItem, ["decision_id"], "")} />
                  <Field label="平仓原因" value={closeReason || (tradeStatus === "open" ? "仍在持仓" : "未记录")} />
                  <Field label="平仓时间" value={formatTime(pick(tradeOutcome, ["close_ts"]))} />
                  <Field
                    label="学习处理"
                    value={translateDisplayValue(tradeLearningStatus)}
                    tone={factBoundTone(replayDisplayFact, learningTone(tradeLearningStatus), replayDisplayFailed)}
                  />
                  <Field label="样本数量" value={formatDecimal(pickNumber(tradeLearning, ["sample_count"], 0), 0)} />
                  <Field label="成熟样本" value={formatDecimal(pickNumber(tradeLearning, ["matured_sample_count"], 0), 0)} />
                </div>
                <div className="evidence-learning-note">
                  <strong>系统会怎么学</strong>
                  <span>{pickString(tradeLearning, ["summary"], "等待回放证据补齐。")}</span>
                  {tradeFactorDetail ? <span>{tradeFactorDetail}</span> : null}
                </div>
              </>
            ) : (
              <div className="empty-state-small">生成回放后显示多空方向、实际盈亏和平仓学习结果</div>
            )}
          </MetricCard>

          <MetricCard title="回放覆盖">
            <div className="evidence-list">
              {metricEntries.map(([name, raw]) => {
                const metric = asRecord(raw);
                const coverage = pickNumber(metric, ["coverage", "agreement_rate"], 0);
                const status = pickString(metric, ["status"], coverage >= 0.95 || coverage >= 95 ? "ok" : "attention");
                return (
                  <div className="evidence-row" key={name}>
                    <div><strong>{translateDisplayValue(name)}</strong><span>{formatDecimal(coverage <= 1 ? coverage * 100 : coverage, 1)}%</span></div>
                    <StatusPill status={translateDisplayValue(status)} tone={toneFromStatus(status)} />
                  </div>
                );
              })}
            </div>
          </MetricCard>

          <MetricCard title="不一致样本">
            <EvidenceRows items={mismatchItems} empty="当前没有回放不一致样本" />
          </MetricCard>

          <details className="detail-disclosure wide-panel">
            <summary><FileSearch2 size={15} aria-hidden="true" />查看完整证据载荷</summary>
            <JsonBlock value={replayReport} />
          </details>
        </div>
        </FactBoundary>
      ) : null}

      {activeTab === "incident" ? (
        <div className="dashboard-grid">
          <MetricCard title="当前事故姿态">
            <FactBoundary fact={incidentFact} label="事故控制事实" requestFailed={incidentFailed}>
            <div className="field-list">
              <Field label="当前模式" value={incidentMode} tone={factBoundTone(incidentFact, incidentMode === "normal" ? "ok" : "warn", incidentFailed)} />
              <Field label="原因" value={pickString(incident, ["reason"], "")} />
              <Field label="更新时间" value={formatTime(pick(incident, ["updated_at", "created_at"]))} />
              <Field label="最近范围收紧" value={pickString(enforcement, ["status"], "")} tone={factBoundTone(enforcementFact, toneFromStatus(pickString(enforcement, ["status"], "")), enforcementFailed)} />
            </div>
            </FactBoundary>
          </MetricCard>

          <MetricCard title="风险收紧">
            <div className="page-action-bar">
              {["normal", "no_new_risk", "only_close", "frozen"].map((mode) => (
                <ActionButton
                  key={mode}
                  icon={SlidersHorizontal}
                  label={translateDisplayValue(mode)}
                  variant={mode === "normal" ? "ghost" : mode === "frozen" ? "danger" : "primary"}
                  disabled={mode === "normal" && !incidentKnown}
                  loading={incidentModeMutation.isPending}
                  error={incidentModeMutation.isError ? "切换失败" : null}
                  confirmMessage={`服务端将事故模式切换为“${translateDisplayValue(mode)}”。`}
                  onAction={() => incidentModeMutation.mutateAsync(mode)}
                />
              ))}
            </div>
          </MetricCard>

          <MetricCard title="应急预案" className="wide-panel">
            <div className="page-action-bar">
              <ActionButton icon={ClipboardList} label="生成预案" variant="primary" loading={playbookMutation.isPending} error={playbookMutation.isError ? "生成失败" : null} onAction={() => playbookMutation.mutateAsync()} />
              <ActionButton icon={CheckCircle2} label="执行健康收紧" variant="danger" loading={enforceMutation.isPending} error={enforceMutation.isError ? "执行失败" : null} onAction={() => enforceMutation.mutateAsync()} />
            </div>
            <div className="evidence-summary">
              <Field label="预案编号" value={pickString(playbook, ["playbook_id"], "")} />
              <Field label="状态" value={pickString(playbook, ["status"], "")} tone={factBoundTone(playbookFact, toneFromStatus(pickString(playbook, ["status"], "")), playbookFailed)} />
              <Field label="目标模式" value={pickString(playbook, ["target_mode"], "")} />
              <Field label="严重级别" value={pickString(playbook, ["severity"], "")} />
            </div>
            <EvidenceRows items={pickArray(playbook, ["steps"])} empty="尚未生成应急预案" />
          </MetricCard>
        </div>
      ) : null}

      {activeTab === "release" ? (
        <div className="dashboard-grid">
          <MetricCard title="发布记录">
            <div className="page-action-bar">
              <ActionButton icon={Rocket} label="开始发布记录" variant="primary" loading={releaseMutation.isPending} error={releaseMutation.isError ? "启动失败" : null} onAction={() => releaseMutation.mutateAsync()} />
            </div>
            <FactBoundary fact={releaseFact} label="发布事实" requestFailed={releaseFailed}>
            <div className="field-list">
              <Field label="发布编号" value={releaseRunId} />
              <Field label="状态" value={pickString(release, ["status"], "")} tone={factBoundTone(releaseFact, toneFromStatus(pickString(release, ["status"], "")), releaseFailed)} />
              <Field label="回放编号" value={pickString(release, ["replay_run_id"], "")} />
              <Field label="就绪姿态" value={pickString(release, ["readiness_posture"], "")} />
              <Field label="创建时间" value={formatTime(pick(release, ["created_at"]))} />
            </div>
            </FactBoundary>
          </MetricCard>

          <MetricCard title="审批轨迹">
            <SectionHead title="当前发布审批" status={`${approvals.length}`} tone={factBoundTone(approvalsFact, approvals.length ? "ok" : "warn", approvalsFailed)} />
            <EvidenceRows items={approvals} empty={releaseRunId ? "当前发布没有审批记录" : "尚未开始发布记录"} />
          </MetricCard>
        </div>
      ) : null}
    </section>
  );
}

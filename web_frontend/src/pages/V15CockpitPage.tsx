import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  BrainCircuit,
  CheckCircle2,
  ClipboardList,
  Gauge,
  GitBranch,
  Layers3,
  Play,
  RefreshCw,
  Rocket,
  ShieldAlert,
  Siren,
  SlidersHorizontal,
  Workflow,
} from "lucide-react";
import {
  enforceAutonomyScope,
  getAutonomousLearningSamples,
  getAutonomyScopeApprovalLatest,
  getAutonomyScopeEnforcementLatest,
  getEvolutionRuns,
  getFactorCatalog,
  getIncidentControl,
  getIncidentPlaybookLatest,
  getLearningApplications,
  getLearningSummary,
  getParameterTemplatesActive,
  getParameterTemplateSwitchLogs,
  getReplayBarDecisions,
  getReleaseApprovals,
  getReleaseLatest,
  getReplayLatest,
  getRiskSummary,
  getV15Phase0,
  runIncidentPlaybook,
  runReplayBarEvidence,
  setIncidentControl,
  startReleaseRun,
} from "@/api/client";
import { ActionButton } from "@/components/ActionButton";
import { factBoundTone, factHasDisplayValue, factIsKnown, readFact, type FactEnvelope } from "@/api/fact";
import { MetricCard } from "@/components/Card";
import { CompactMetric, Field, ProgressMetric, SectionHead, StatTile, toneFromStatus, type Tone } from "@/components/DashboardBits";
import { JsonBlock } from "@/components/JsonBlock";
import { StatusPill } from "@/components/StatusPill";
import { useBackendReadinessQuery } from "@/hooks/useCoreQueries";
import { queryKeys } from "@/api/queryKeys";
import { asRecord, isRecord, pick, pickArray, pickBoolean, pickNumber, pickRecord, pickString } from "@/lib/compat";
import { translateDisplayValue, translateReasonText } from "@/lib/display";
import { formatDecimal, formatTime } from "@/lib/format";

type TabKey = "runtime" | "factors" | "governance" | "replay" | "risk" | "learning" | "incidents" | "release";

const tabs: Array<{ key: TabKey; label: string; icon: typeof Activity }> = [
  { key: "runtime", label: "运行态", icon: Activity },
  { key: "factors", label: "因子", icon: Layers3 },
  { key: "governance", label: "治理", icon: GitBranch },
  { key: "replay", label: "回放", icon: Workflow },
  { key: "risk", label: "风控", icon: ShieldAlert },
  { key: "learning", label: "学习", icon: BrainCircuit },
  { key: "incidents", label: "事故", icon: Siren },
  { key: "release", label: "发布", icon: Rocket },
];

const replayMetricLabels: Record<string, string> = {
  bar_replay: "K线窗口回放",
  factor_frame_replay: "因子帧重建",
  execution_gate_recompute: "执行闸门复算",
  risk_policy_recompute: "风控策略复算",
  order_lifecycle_replay: "订单生命周期",
  order_outcome_causality_replay: "订单结果因果链",
  broker_fill_slippage_replay: "成交滑点核对",
  supervisor_counterfactual_replay: "监督反事实",
  risk_policy_subaction_replay: "风控子动作复算",
};

function clampPct(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.min(Math.max(value, 0), 100);
}

function scorePct(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return value <= 1 ? value * 100 : value;
}

function metricTone(value: number, warn = 70, bad = 90): Tone {
  if (!Number.isFinite(value) || value <= 0) return "mute";
  if (value >= bad) return "bad";
  if (value >= warn) return "warn";
  return "ok";
}

function boolTone(value: boolean): Tone {
  return value ? "ok" : "warn";
}

/** Keep retained values from the few legacy endpoints without fact.v1 visible,
 * but never present a success colour as if freshness were proven. */
function unverifiedTone(tone: Tone): Tone {
  return tone === "ok" ? "warn" : tone;
}

function endpointTone(
  tone: Tone,
  fact?: FactEnvelope,
  requestFailed = false,
): Tone {
  return fact ? factBoundTone(fact, tone, requestFailed) : unverifiedTone(tone);
}

function safeLabel(value: unknown): string {
  if (value === undefined || value === null || value === "") return "";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return translateDisplayValue(String(value));
  const record = asRecord(value);
  const direct = pickString(record, ["label", "name", "id", "key", "status", "reason", "message"], "");
  return direct ? translateReasonText(direct) : "";
}

function DataList({
  items,
  empty = "无记录",
  fact,
  requestFailed = false,
}: {
  items: unknown[];
  empty?: string;
  fact?: FactEnvelope;
  requestFailed?: boolean;
}) {
  if (!items.length) {
    return <div className="empty-state-small">{empty}</div>;
  }
  return (
    <div className="v15-list">
      {items.slice(0, 8).map((item, index) => {
        const record = asRecord(item);
        const title = pickString(record, ["title", "name", "factor", "run_id", "decision_id", "event_id", "counterfactual_id", "suggestion_id", "id"], `#${index + 1}`);
        const status = pickString(record, ["status", "state", "decision", "label", "evidence_grade", "reason"], "");
        const detail = pickString(record, ["summary", "message", "reason", "action", "event_type", "run_type", "scope_type"], "");
        const ts = pick(record, ["created_at", "updated_at", "started_at", "event_ts", "ts"]);
        return (
          <div className="v15-list-row" key={`${title}-${index}`}>
            <div>
              <strong>{title}</strong>
              <span>{detail ? translateReasonText(detail) : formatTime(ts)}</span>
            </div>
            {status ? <StatusPill status={status} tone={endpointTone(toneFromStatus(status), fact, requestFailed)} /> : null}
          </div>
        );
      })}
    </div>
  );
}

function isCatalogFactorRecord(item: unknown): boolean {
  if (!isRecord(item)) {
    return false;
  }
  const explicitId = pickString(item, ["factor_id"], "");
  if (explicitId) {
    return true;
  }
  const legacyId = pickString(item, ["factor"], "");
  if (!legacyId) {
    return false;
  }
  return (
    pickString(item, ["role"], "") !== "" ||
    pickString(item, ["source"], "") !== "" ||
    pickString(item, ["health_status"], "") !== "" ||
    pickString(item, ["lifecycle_status"], "") !== "" ||
    pick(item, ["weight"]) !== undefined ||
    pick(item, ["eligible_for_live"]) !== undefined ||
    pick(item, ["used_in_score"]) !== undefined
  );
}

function factorIdOf(item: unknown): string {
  const explicitId = pickString(item, ["factor_id"], "");
  if (explicitId) {
    return explicitId;
  }
  return isCatalogFactorRecord(item) ? pickString(item, ["factor"], "") : "";
}

function extractFactorCatalogItems(input: unknown): Record<string, unknown>[] {
  // ``GET /api/factor-v4/catalog`` exposes its catalog at the top-level
  // ``items`` field in both live and latest-snapshot modes.  Do not search
  // recursively: a nested advisory payload must never be mistaken for the
  // endpoint's authoritative catalog.
  const endpoint = isRecord(input) ? input : {};
  const items = Array.isArray(endpoint.items) ? endpoint.items : [];
  const records = items.filter(isRecord).filter(isCatalogFactorRecord);
  const byFactorId = new Map<string, Record<string, unknown>>();
  for (const item of records) {
    const factorId = factorIdOf(item);
    if (!factorId) {
      continue;
    }
    const previous = byFactorId.get(factorId);
    if (!previous) {
      byFactorId.set(factorId, item);
      continue;
    }
    const itemScore =
      Number(pickBoolean(item, ["used_in_score"], false)) * 4 +
      Number(pickBoolean(item, ["eligible_for_live"], false)) * 3 +
      Number(pickString(item, ["source"], "") === "builtin") * 2 +
      Number(Math.abs(pickNumber(item, ["weight"], 0)) > 0);
    const previousScore =
      Number(pickBoolean(previous, ["used_in_score"], false)) * 4 +
      Number(pickBoolean(previous, ["eligible_for_live"], false)) * 3 +
      Number(pickString(previous, ["source"], "") === "builtin") * 2 +
      Number(Math.abs(pickNumber(previous, ["weight"], 0)) > 0);
    if (itemScore > previousScore) {
      byFactorId.set(factorId, item);
    }
  }
  return [...byFactorId.values()];
}

function isOperationalFactor(item: unknown): boolean {
  const source = pickString(item, ["source"], "");
  return pickBoolean(item, ["used_in_score"], false) || source === "builtin";
}

function FactorCatalogList({
  items,
  fact,
  requestFailed = false,
}: {
  items: unknown[];
  fact: FactEnvelope;
  requestFailed?: boolean;
}) {
  const records = items.map(asRecord).filter((item) => factorIdOf(item));
  if (!records.length) {
    return <div className="empty-state-small">因子目录暂无条目</div>;
  }
  return (
    <div className="v15-list">
      {records.slice(0, 12).map((item, index) => {
        const factorId = factorIdOf(item) || `#${index + 1}`;
        const role = pickString(item, ["role"], "");
        const source = pickString(item, ["source"], "");
        const enabled = pickBoolean(item, ["enabled"], false);
        const eligible = pickBoolean(item, ["eligible_for_live"], false);
        const usedInScore = pickBoolean(item, ["used_in_score"], false);
        const health = pickString(item, ["health_status"], "UNKNOWN");
        const weight = pickNumber(item, ["weight"], 0);
        const reason = pickString(item, ["reason_excluded"], "");
        return (
          <div className="v15-list-row" key={`${factorId}-${index}`}>
            <div>
              <strong>{factorId}</strong>
              <span>
                {translateDisplayValue(role)} / {translateDisplayValue(source)} / 权重 {formatDecimal(weight, 3)}
                {reason ? ` / ${translateReasonText(reason)}` : ""}
              </span>
            </div>
            <div className="v15-pill-row">
              <StatusPill status={enabled ? "已启用" : "未启用"} tone={endpointTone(enabled ? "ok" : "warn", fact, requestFailed)} />
              <StatusPill status={usedInScore ? "参与打分" : eligible ? "可用" : "观察"} tone={endpointTone(usedInScore ? "ok" : eligible ? "warn" : "mute", fact, requestFailed)} />
              <StatusPill status={health} tone={endpointTone(toneFromStatus(health), fact, requestFailed)} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

function summaryValue(summary: Record<string, unknown>, keys: string[]): string {
  for (const key of keys) {
    const value = pick(summary, [key]);
    if (typeof value === "number" && Number.isFinite(value)) {
      return `${translateDisplayValue(key)} ${formatDecimal(value, 0)}`;
    }
    if (typeof value === "string" && value) {
      return `${translateDisplayValue(key)} ${translateDisplayValue(value)}`;
    }
  }
  return "";
}

function evolutionRunDetail(item: Record<string, unknown>): string {
  const summary = asRecord(pick(item, ["summary"]));
  const parts = [
    translateDisplayValue(pickString(item, ["run_type"], "治理运行")),
    translateDisplayValue(pickString(item, ["trigger_source"], "")),
    summaryValue(summary, [
      "repaired",
      "suggestions",
      "stats_upserted",
      "checked",
      "approved",
      "skipped",
      "schema_version",
    ]),
    formatTime(pick(item, ["started_at"])),
  ].filter(Boolean);
  return parts.join(" / ");
}

function GovernanceRunList({ items }: { items: unknown[] }) {
  const records = items.map(asRecord).filter((item) => pickString(item, ["run_id"], ""));
  if (!records.length) {
    return <div className="empty-state-small">暂无治理运行记录</div>;
  }
  return (
    <div className="v15-list">
      {records.slice(0, 8).map((item, index) => {
        const runId = pickString(item, ["run_id"], `#${index + 1}`);
        const status = pickString(item, ["status"], "");
        return (
          <div className="v15-list-row" key={`${runId}-${index}`}>
            <div>
              <strong>{runId}</strong>
              <span>{evolutionRunDetail(item)}</span>
            </div>
            {status ? <StatusPill status={status} tone={unverifiedTone(toneFromStatus(status))} /> : null}
          </div>
        );
      })}
    </div>
  );
}

function TemplateList({
  items,
  fact,
  requestFailed = false,
}: {
  items: unknown[];
  fact?: FactEnvelope;
  requestFailed?: boolean;
}) {
  const records = items.map(asRecord);
  if (!records.length) {
    return <div className="empty-state-small">暂无模板记录</div>;
  }
  return (
    <div className="v15-list">
      {records.slice(0, 8).map((item, index) => {
        const factorId = pickString(item, ["factor_id"], `#${index + 1}`);
        const templateId = pickString(item, ["template_id", "active_template_id", "target_template_id"], "");
        const status = pickString(item, ["status", "active"], "");
        const detail = [
          templateId,
          pickString(item, ["regime_key"], ""),
          formatTime(pick(item, ["updated_at", "created_at", "switched_at"])),
        ].filter(Boolean).join(" / ");
        return (
          <div className="v15-list-row" key={`${factorId}-${templateId}-${index}`}>
            <div>
              <strong>{factorId}</strong>
              <span>{detail || ""}</span>
            </div>
            {status ? <StatusPill status={status} tone={endpointTone(toneFromStatus(status), fact, requestFailed)} /> : null}
          </div>
        );
      })}
    </div>
  );
}

function KlineWindowPreview({ preview, pending = false }: { preview: unknown; pending?: boolean }) {
  const previewRecord = asRecord(preview);
  const windows = pickArray(previewRecord, ["windows"]).map(asRecord);
  if (pending && !windows.length) {
    return <div className="v15-kline v15-kline-empty">正在生成K线窗口...</div>;
  }
  if (!windows.length) {
    return <div className="v15-kline v15-kline-empty">暂无K线窗口预览</div>;
  }
  const firstWindow = windows[0];
  const bars = pickArray(firstWindow, ["bars"]).map(asRecord);
  if (!bars.length) {
    return <div className="empty-state-small">K线窗口没有可画的OHLC数据</div>;
  }
  const width = 720;
  const height = 220;
  const pad = 18;
  const highs = bars.map((bar) => pickNumber(bar, ["high"], 0)).filter(Number.isFinite);
  const lows = bars.map((bar) => pickNumber(bar, ["low"], 0)).filter(Number.isFinite);
  const maxPrice = Math.max(...highs);
  const minPrice = Math.min(...lows);
  const span = Math.max(0.000001, maxPrice - minPrice);
  const xStep = bars.length > 1 ? (width - pad * 2) / (bars.length - 1) : width - pad * 2;
  const candleWidth = Math.max(3, Math.min(9, xStep * 0.55));
  const yFor = (price: number) => pad + ((maxPrice - price) / span) * (height - pad * 2);
  const decisionTs = pickNumber(firstWindow, ["decision_ts"], 0);
  const decisionIndex = bars.reduce((best, bar, index) => {
    const currentTime = pickNumber(bar, ["time"], 0);
    const bestTime = pickNumber(bars[best], ["time"], 0);
    return Math.abs(currentTime - decisionTs) < Math.abs(bestTime - decisionTs) ? index : best;
  }, 0);
  const decisionBar = asRecord(bars[decisionIndex]);
  const beforeBars = bars.filter((bar) => pickNumber(bar, ["time"], 0) <= decisionTs);
  const afterBars = bars.filter((bar) => pickNumber(bar, ["time"], 0) > decisionTs);
  const priorBar = beforeBars.length > 1 ? asRecord(beforeBars[beforeBars.length - 2]) : {};
  const decisionClose = pickNumber(decisionBar, ["close"], 0);
  const priorClose = pickNumber(priorBar, ["close"], decisionClose);
  const futureHigh = afterBars.length ? Math.max(...afterBars.map((bar) => pickNumber(bar, ["high"], decisionClose))) : decisionClose;
  const futureLow = afterBars.length ? Math.min(...afterBars.map((bar) => pickNumber(bar, ["low"], decisionClose))) : decisionClose;
  const finalClose = afterBars.length ? pickNumber(afterBars[afterBars.length - 1], ["close"], decisionClose) : decisionClose;
  const futureUp = futureHigh - decisionClose;
  const futureDown = futureLow - decisionClose;
  const finalMove = finalClose - decisionClose;
  const move = decisionClose - priorClose;
  const windowPosition = span > 0 ? ((decisionClose - minPrice) / span) * 100 : 0;
  const reading = move > 0 ? "决策K线收涨" : move < 0 ? "决策K线收跌" : "决策K线横盘";
  const location =
    windowPosition >= 70 ? "价格位于窗口高位" :
      windowPosition <= 30 ? "价格位于窗口低位" :
        "价格位于窗口中段";

  return (
    <div className="v15-kline">
      <div className="v15-section-head">
        <h3>{pickString(firstWindow, ["symbol"], "XAUUSD+")} / {pickString(firstWindow, ["timeframe"], "M5")}</h3>
        <StatusPill status={`${formatDecimal(bars.length, 0)} 根K线`} tone="warn" />
      </div>
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="K线回放窗口预览">
        <line x1={pad} x2={width - pad} y1={height - pad} y2={height - pad} className="v15-kline-axis" />
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
            <g key={`${pickNumber(bar, ["time"], index)}-${index}`} className={up ? "v15-candle-up" : "v15-candle-down"}>
              <line x1={x} x2={x} y1={yFor(high)} y2={yFor(low)} />
              <rect x={x - candleWidth / 2} y={bodyTop} width={candleWidth} height={bodyHeight} rx="1" />
            </g>
          );
        })}
        {decisionTs ? (
          <>
            <rect
              x={pad + decisionIndex * xStep}
              y={pad}
              width={Math.max(0, width - pad - (pad + decisionIndex * xStep))}
              height={height - pad * 2}
              className="v15-kline-future"
            />
          <line
            x1={pad + decisionIndex * xStep}
            x2={pad + decisionIndex * xStep}
            y1={pad}
            y2={height - pad}
            className="v15-kline-decision"
          />
          </>
        ) : null}
      </svg>
      <div className="v15-kline-meta">
        <span>最高 {formatDecimal(maxPrice, 2)}</span>
        <span>最低 {formatDecimal(minPrice, 2)}</span>
        <span>决策时间 {formatTime(decisionTs)}</span>
        <span>后续K线 {formatDecimal(afterBars.length, 0)} 根</span>
      </div>
      <div className="v15-replay-reading">
        <strong>这次回放能看什么</strong>
        <span>{reading}，{location}；蓝色虚线是系统做出这次决策的K线位置，淡蓝区域是决策后的走势。</span>
        <span>决策后最高上冲 {formatDecimal(futureUp, 2)}，最大下探 {formatDecimal(futureDown, 2)}，最后变化 {formatDecimal(finalMove, 2)}。</span>
      </div>
    </div>
  );
}

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

function TradeOutcomeLearningPanel({
  preview,
  fact,
  requestFailed = false,
}: {
  preview: unknown;
  fact: FactEnvelope;
  requestFailed?: boolean;
}) {
  const previewRecord = asRecord(preview);
  const items = pickArray(previewRecord, ["items"]).map(asRecord);
  if (!items.length) {
    return <div className="empty-state-small">暂无交易结果与学习证据</div>;
  }
  const item = items[0];
  const outcome = asRecord(pick(item, ["outcome"]));
  const learning = asRecord(pick(item, ["learning"]));
  const result = pickString(outcome, ["result"], "");
  const status = pickString(outcome, ["status"], "");
  const learningStatus = pickString(learning, ["status"], "");
  const pnl = pickNumber(outcome, ["pnl"], 0);
  const pnlText =
    status === "no_trade" ? "未交易" :
      status === "open" ? "未平仓" :
        status === "missing" ? "" :
          `${pnl >= 0 ? "+" : ""}${formatDecimal(pnl, 2)}`;
  const tradeRef = pickString(item, ["position_id"], "") || pickString(item, ["trade_id"], "");
  const tradeTitle = tradeRef ? `仓位 ${tradeRef}` : pickString(item, ["decision_id"], "");
  const directionLabel = translateDisplayValue(pickString(item, ["direction_label"], ""));
  const label = translateDisplayValue(pickString(outcome, ["outcome_label"], "") || result || status);
  const closeReason = translateDisplayValue(pickString(outcome, ["close_reason"], ""));
  const sampleDetail = [
    `样本 ${formatDecimal(pickNumber(learning, ["sample_count"], 0), 0)}`,
    `成熟 ${formatDecimal(pickNumber(learning, ["matured_sample_count"], 0), 0)}`,
    pickString(learning, ["latest_sample_type"], ""),
  ].filter(Boolean).join(" / ");
  const factorDetail = [
    pickString(outcome, ["primary_factor"], "") ? `主因 ${pickString(outcome, ["primary_factor"], "")}` : "",
    pickString(outcome, ["worst_factor"], "") ? `拖累 ${pickString(outcome, ["worst_factor"], "")}` : "",
  ].filter(Boolean).join(" / ");

  return (
    <div className="v15-outcome-panel">
      <div className="v15-section-head">
        <h3>交易结果与学习</h3>
        <StatusPill status={translateDisplayValue(learningStatus || status)} tone={endpointTone(learningTone(learningStatus), fact, requestFailed)} />
      </div>
      <div className="v15-mini-grid">
        <CompactMetric label="开仓方向" value={directionLabel} detail={pickString(item, ["symbol"], "") || ""} tone={endpointTone(directionLabel === "" ? "mute" : "ok", fact, requestFailed)} />
        <CompactMetric label="这单结果" value={translateDisplayValue(result || status)} detail={label} tone={endpointTone(outcomeTone(result, status), fact, requestFailed)} />
        <CompactMetric label="实际盈亏" value={pnlText} detail={tradeTitle} tone={endpointTone(outcomeTone(result, status), fact, requestFailed)} />
        <CompactMetric label="平仓原因" value={closeReason} detail={formatTime(pick(outcome, ["close_ts"]))} tone={closeReason === "" ? "mute" : "warn"} />
        <CompactMetric label="学习处理" value={translateDisplayValue(learningStatus)} detail={sampleDetail || ""} tone={endpointTone(learningTone(learningStatus), fact, requestFailed)} />
      </div>
      <div className="v15-replay-reading">
        <strong>系统会怎么学</strong>
        <span>{pickString(learning, ["summary"], "等待回放证据补齐。")}</span>
        {factorDetail ? <span>{factorDetail}</span> : null}
      </div>
    </div>
  );
}

function replayDecisionLabel(item: Record<string, unknown>): string {
  const tradeRef = pickString(item, ["position_id"], "") || pickString(item, ["trade_id"], "");
  const direction = translateDisplayValue(pickString(item, ["direction_label"], ""));
  const result = pickString(item, ["outcome_result"], "") || pickString(item, ["outcome_status"], "");
  const pnl = pickNumber(item, ["pnl"], 0);
  const pnlText = result === "profit" || result === "loss" || result === "flat" ? ` ${pnl >= 0 ? "+" : ""}${formatDecimal(pnl, 2)}` : "";
  const pieces = [
    formatTime(pick(item, ["decision_ts"])),
    `${pickString(item, ["symbol"], "XAUUSD+")}/${pickString(item, ["timeframe"], "M5")}`,
    direction,
    tradeRef ? `仓位 ${tradeRef}` : translateDisplayValue(pickString(item, ["event_type"], "")),
    `${translateDisplayValue(result)}${pnlText}`,
  ];
  return pieces.filter(Boolean).join(" / ");
}

function ReplayDecisionPicker({
  items,
  value,
  onChange,
}: {
  items: Record<string, unknown>[];
  value: string;
  onChange: (value: string) => void;
}) {
  const selected = items.find((item) => pickString(item, ["decision_id"], "") === value);
  return (
    <div className="v15-replay-picker">
      <label htmlFor="v15-replay-decision">历史选择</label>
      <select id="v15-replay-decision" value={value} onChange={(event) => onChange(event.target.value)}>
        <option value="latest_trade">最近真实交易</option>
        {items.map((item) => {
          const decisionId = pickString(item, ["decision_id"], "");
          return (
            <option key={decisionId} value={decisionId}>
              {replayDecisionLabel(item)}
            </option>
          );
        })}
      </select>
      <span>{selected ? `当前选择：${replayDecisionLabel(selected)}` : "当前选择：最近真实交易"}</span>
    </div>
  );
}

function BarWindowIssueList({ items }: { items: unknown[] }) {
  const records = items.map(asRecord);
  if (!records.length) {
    return <div className="empty-state-small">无K线窗口缺口</div>;
  }
  return (
    <div className="v15-list">
      {records.slice(0, 6).map((item, index) => {
        const decisionId = pickString(item, ["decision_id"], `#${index + 1}`);
        const issues = pickArray(item, ["issues"]).map((issue) => translateDisplayValue(issue)).join("、") || "";
        const detail = [
          `${pickString(item, ["symbol"], "XAUUSD+")}/${pickString(item, ["timeframe"], "M5")}`,
          `K线 ${formatDecimal(pickNumber(item, ["bar_count"], 0), 0)}`,
          `决策前 ${formatDecimal(pickNumber(item, ["before_count"], 0), 0)}`,
          `决策后 ${formatDecimal(pickNumber(item, ["after_count"], 0), 0)}`,
        ].join(" / ");
        return (
          <div className="v15-list-row" key={`${decisionId}-${index}`}>
            <div>
              <strong>{decisionId}</strong>
              <span>{detail}</span>
            </div>
            <StatusPill status={issues} tone="warn" />
          </div>
        );
      })}
    </div>
  );
}

function barWindowIssues(items: unknown[], mode: "blocking" | "notice"): unknown[] {
  return items.filter((item) => {
    const issues = pickArray(item, ["issues"]).map((issue) => String(issue));
    const blocking = issues.some((issue) => ["missing_bar_window", "no_bar_before_decision", "stale_bar_alignment"].includes(issue));
    return mode === "blocking" ? blocking : !blocking && issues.includes("short_bar_window");
  });
}

async function runAndDiscard(action: Promise<unknown>): Promise<void> {
  await action;
}

export function V15CockpitPage() {
  const [activeTab, setActiveTab] = useState<TabKey>("runtime");
  const [selectedReplayDecisionId, setSelectedReplayDecisionId] = useState("latest_trade");
  const queryClient = useQueryClient();

  const readinessQuery = useBackendReadinessQuery();
  const phase0Query = useQuery({ queryKey: ["v15", "phase0"], queryFn: getV15Phase0, refetchInterval: 30_000, staleTime: 10_000 });
  const replayQuery = useQuery({ queryKey: ["v15", "replay-latest"], queryFn: getReplayLatest, refetchInterval: 30_000, staleTime: 10_000 });
  const replayChoicesQuery = useQuery({ queryKey: ["v15", "replay-bar-decisions"], queryFn: () => getReplayBarDecisions(30), refetchInterval: 60_000, staleTime: 20_000 });
  const catalogQuery = useQuery({ queryKey: ["v15", "factor-catalog"], queryFn: () => getFactorCatalog(false), refetchInterval: 30_000, staleTime: 10_000 });
  const catalogSnapshotQuery = useQuery({ queryKey: ["v15", "factor-catalog-snapshot"], queryFn: () => getFactorCatalog(true), refetchInterval: 60_000, staleTime: 20_000 });
  const riskQuery = useQuery({ queryKey: ["v15", "risk-summary"], queryFn: getRiskSummary, refetchInterval: 15_000, staleTime: 5_000 });
  const learningSummaryQuery = useQuery({ queryKey: ["v15", "learning-summary"], queryFn: getLearningSummary, refetchInterval: 30_000, staleTime: 10_000 });
  const learningApplicationsQuery = useQuery({ queryKey: ["v15", "learning-applications"], queryFn: () => getLearningApplications(12), refetchInterval: 30_000, staleTime: 10_000 });
  const learningSamplesQuery = useQuery({ queryKey: ["v15", "learning-samples"], queryFn: () => getAutonomousLearningSamples(12), refetchInterval: 30_000, staleTime: 10_000 });
  const evolutionQuery = useQuery({ queryKey: ["v15", "evolution-runs"], queryFn: () => getEvolutionRuns(10), refetchInterval: 45_000, staleTime: 15_000 });
  const activeTemplatesQuery = useQuery({ queryKey: ["v15", "templates-active"], queryFn: getParameterTemplatesActive, refetchInterval: 60_000, staleTime: 20_000 });
  const templateLogsQuery = useQuery({ queryKey: ["v15", "template-switch-logs"], queryFn: () => getParameterTemplateSwitchLogs(12), refetchInterval: 60_000, staleTime: 20_000 });
  const incidentControlQuery = useQuery({ queryKey: ["v15", "incident-control"], queryFn: getIncidentControl, refetchInterval: 15_000, staleTime: 5_000 });
  const incidentPlaybookQuery = useQuery({ queryKey: ["v15", "incident-playbook"], queryFn: getIncidentPlaybookLatest, refetchInterval: 30_000, staleTime: 10_000 });
  const scopeApprovalQuery = useQuery({ queryKey: ["v15", "scope-approval"], queryFn: getAutonomyScopeApprovalLatest, refetchInterval: 45_000, staleTime: 15_000 });
  const scopeEnforcementQuery = useQuery({ queryKey: ["v15", "scope-enforcement"], queryFn: getAutonomyScopeEnforcementLatest, refetchInterval: 45_000, staleTime: 15_000 });
  const releaseQuery = useQuery({ queryKey: ["v15", "release-latest"], queryFn: getReleaseLatest, refetchInterval: 30_000, staleTime: 10_000 });

  const release = asRecord(pick(releaseQuery.data, ["release"]));
  const releaseRunId = pickString(release, ["run_id"], "");
  const releaseApprovalsQuery = useQuery({
    queryKey: ["v15", "release-approvals", releaseRunId],
    queryFn: () => getReleaseApprovals(releaseRunId),
    enabled: !!releaseRunId,
    refetchInterval: 45_000,
    staleTime: 15_000,
  });

  const readinessRequestFailed = readinessQuery.isError || readinessQuery.isRefetchError;
  const phase0RequestFailed = phase0Query.isError || phase0Query.isRefetchError;
  const replayRequestFailed = replayQuery.isError || replayQuery.isRefetchError;
  const replayChoicesRequestFailed = replayChoicesQuery.isError || replayChoicesQuery.isRefetchError;
  const catalogRequestFailed = catalogQuery.isError || catalogQuery.isRefetchError;
  const catalogSnapshotRequestFailed = catalogSnapshotQuery.isError || catalogSnapshotQuery.isRefetchError;
  const riskRequestFailed = riskQuery.isError || riskQuery.isRefetchError;
  const learningSummaryRequestFailed = learningSummaryQuery.isError || learningSummaryQuery.isRefetchError;
  const learningApplicationsRequestFailed = learningApplicationsQuery.isError || learningApplicationsQuery.isRefetchError;
  const learningSamplesRequestFailed = learningSamplesQuery.isError || learningSamplesQuery.isRefetchError;
  const activeTemplatesRequestFailed = activeTemplatesQuery.isError || activeTemplatesQuery.isRefetchError;
  const incidentControlRequestFailed = incidentControlQuery.isError || incidentControlQuery.isRefetchError;
  const incidentPlaybookRequestFailed = incidentPlaybookQuery.isError || incidentPlaybookQuery.isRefetchError;
  const scopeApprovalRequestFailed = scopeApprovalQuery.isError || scopeApprovalQuery.isRefetchError;
  const scopeEnforcementRequestFailed = scopeEnforcementQuery.isError || scopeEnforcementQuery.isRefetchError;
  const releaseRequestFailed = releaseQuery.isError || releaseQuery.isRefetchError;
  const releaseApprovalsRequestFailed = releaseApprovalsQuery.isError || releaseApprovalsQuery.isRefetchError;
  const readinessFact = readFact(readinessQuery.data, "ops.backend-readiness.v2");
  const phase0Fact = readFact(phase0Query.data, "ops.v15-phase0-completion.v2");
  const replayFact = readFact(replayQuery.data, "ops.replay-latest.v2");
  const replayChoicesFact = readFact(replayChoicesQuery.data, "ops.replay-bar-decisions.v2");
  const catalogFact = readFact(catalogQuery.data, "factor.catalog.v4");
  const catalogSnapshotFact = readFact(catalogSnapshotQuery.data, "factor.catalog.v4");
  const riskFact = readFact(riskQuery.data, "risk.summary.v2");
  const learningSummaryFact = readFact(learningSummaryQuery.data, "learning.summary.v2");
  const learningApplicationsFact = readFact(learningApplicationsQuery.data, "learning.applications.v2");
  const learningSamplesFact = readFact(learningSamplesQuery.data, "learning.autonomous-samples.v2");
  const activeTemplatesFact = readFact(activeTemplatesQuery.data, "learning.parameter-templates-active.v2");
  const incidentControlFact = readFact(incidentControlQuery.data, "ops.incident-control.v2");
  const incidentPlaybookFact = readFact(incidentPlaybookQuery.data, "ops.incident-playbook-latest.v2");
  const scopeApprovalFact = readFact(scopeApprovalQuery.data, "ops.autonomy-scope-approval-latest.v2");
  const scopeEnforcementFact = readFact(scopeEnforcementQuery.data, "ops.autonomy-scope-enforcement-latest.v2");
  const releaseFact = readFact(releaseQuery.data, "ops.release-latest.v2");
  const releaseApprovalsFact = readFact(releaseApprovalsQuery.data, "ops.release-approval-trail.v2");
  const readinessKnown = factIsKnown(readinessFact, readinessRequestFailed);
  const phase0Known = factIsKnown(phase0Fact, phase0RequestFailed);
  const riskKnown = factIsKnown(riskFact, riskRequestFailed);
  const incidentControlKnown = factIsKnown(incidentControlFact, incidentControlRequestFailed);

  const refreshAll = () => {
    void queryClient.invalidateQueries({ queryKey: ["v15"] });
  };

  const runReplayMutation = useMutation({
    mutationFn: (decisionId: string) => runReplayBarEvidence(decisionId === "latest_trade" ? "" : decisionId),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["v15", "replay-latest"] }),
  });
  const enforceScopeMutation = useMutation({
    mutationFn: enforceAutonomyScope,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["v15", "scope-enforcement"] });
      void queryClient.invalidateQueries({ queryKey: ["v15", "incident-control"] });
      void queryClient.invalidateQueries({ queryKey: queryKeys.readiness });
    },
  });
  const playbookMutation = useMutation({
    mutationFn: () => runIncidentPlaybook("governance_failure", "medium"),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["v15", "incident-playbook"] }),
  });
  const releaseStartMutation = useMutation({
    mutationFn: startReleaseRun,
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["v15", "release-latest"] }),
  });
  const incidentModeMutation = useMutation({
    mutationFn: (mode: string) => setIncidentControl(mode, `web_v15_cockpit:${mode}`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["v15", "incident-control"] });
      void queryClient.invalidateQueries({ queryKey: queryKeys.readiness });
    },
  });
  const replayPreviewFact = readFact(runReplayMutation.data, "ops.replay-bar-preview.v2");

  const readiness = asRecord(readinessQuery.data);
  const v15 = asRecord(pick(readiness, ["v15"]));
  const autonomyHealth = asRecord(pick(readiness, ["autonomy_health"]));
  const incidentControl = asRecord(pick(incidentControlQuery.data, ["incident_control"]));
  const phase0 = asRecord(pick(phase0Query.data, ["phase0"]));
  const latestReplay = asRecord(pick(replayQuery.data, ["replay", "latest_report", "report"]));
  const replayRunReport = asRecord(pick(runReplayMutation.data, ["report"]));
  const hasReplayRunReport = !!pickString(replayRunReport, ["replay_run_id"], "");
  const replayDisplayReport = hasReplayRunReport ? replayRunReport : latestReplay;
  const replayDisplayFact = hasReplayRunReport ? replayPreviewFact : replayFact;
  const replayDisplayRequestFailed = hasReplayRunReport ? runReplayMutation.isError : replayRequestFailed;
  const replayChoices = useMemo(() => {
    if (!factHasDisplayValue(replayChoicesFact)) return [];
    const direct = pickArray(replayChoicesQuery.data, ["items"]).map(asRecord);
    if (direct.length) return direct;
    return pickArray(pick(replayChoicesQuery.data, ["choices"]), ["items"]).map(asRecord);
  }, [replayChoicesFact, replayChoicesQuery.data]);
  const replayMetrics = asRecord(pick(replayDisplayReport, ["metric_summary"]));
  const barReplayMetrics = asRecord(pick(replayMetrics, ["bar_replay"]));
  const barWindowPreview = pick(replayMetrics, ["bar_window_preview"]);
  const tradeOutcomeLearningPreview = pick(replayMetrics, ["trade_outcome_learning_preview"]);
  const factorCatalog = asRecord(catalogQuery.data);
  const snapshotCatalog = asRecord(catalogSnapshotQuery.data);
  const liveCatalogItems = useMemo(() => {
    return extractFactorCatalogItems(catalogQuery.data);
  }, [catalogQuery.data]);
  const snapshotCatalogItems = useMemo(() => {
    return extractFactorCatalogItems(catalogSnapshotQuery.data);
  }, [catalogSnapshotQuery.data]);
  const liveCatalogDisplayable = factHasDisplayValue(catalogFact);
  const snapshotCatalogDisplayable = factHasDisplayValue(catalogSnapshotFact);
  const catalogItems = useMemo(() => {
    if (liveCatalogDisplayable) return liveCatalogItems;
    if (snapshotCatalogDisplayable) return snapshotCatalogItems;
    return [];
  }, [liveCatalogDisplayable, liveCatalogItems, snapshotCatalogDisplayable, snapshotCatalogItems]);
  const catalogDisplayFact = liveCatalogDisplayable ? catalogFact : snapshotCatalogDisplayable ? catalogSnapshotFact : catalogFact;
  const catalogDisplayRequestFailed = liveCatalogDisplayable
    ? catalogRequestFailed
    : snapshotCatalogDisplayable
      ? catalogSnapshotRequestFailed
      : catalogRequestFailed || catalogSnapshotRequestFailed;
  const catalogSourceLabel = liveCatalogDisplayable ? "实时目录" : snapshotCatalogDisplayable ? "最近快照" : "事实未知";
  const operationalCatalogItems = useMemo(() => {
    const filtered = catalogItems.filter(isOperationalFactor);
    return filtered.length ? filtered : catalogItems;
  }, [catalogItems]);
  const risk = asRecord(riskQuery.data);
  const learningSummary = asRecord(learningSummaryQuery.data);
  const latestPlaybook = asRecord(pick(incidentPlaybookQuery.data, ["playbook"]));
  const latestScopeApproval = asRecord(pick(scopeApprovalQuery.data, ["approval_event"]));
  const latestScopeEnforcement = asRecord(pick(scopeEnforcementQuery.data, ["enforcement_event"]));
  const releaseApprovals = pickArray(releaseApprovalsQuery.data, ["events", "approvals", "approval_events"]);

  const readyForFrontend = pickBoolean(readiness, ["ready_for_frontend", "ok"], false);
  const healthScore = scorePct(pickNumber(autonomyHealth, ["score"], 0));
  const healthPosture = pickString(autonomyHealth, ["posture"], "");
  const incidentMode = pickString(incidentControl, ["mode"], "");
  const latestReplayGrade = pickString(replayDisplayReport, ["evidence_grade"], "");
  const replayMismatch = pickNumber(replayDisplayReport, ["mismatch_count"], 0);
  const replayError = pickString(replayDisplayReport, ["replay_error"], "");
  const releaseStatus = pickString(release, ["status"], "");
  const phase0Complete = pickBoolean(phase0, ["implementation_complete"], false);
  const operationallyReady = pickBoolean(phase0, ["operationally_ready"], false);
  const blockers = pickArray(readiness, ["blockers"]);

  const catalogCount = catalogItems.length || pickNumber(liveCatalogDisplayable ? factorCatalog : snapshotCatalogDisplayable ? snapshotCatalog : {}, ["count"], 0);
  const operationalCount = operationalCatalogItems.length;
  const alphaFactors = operationalCatalogItems.filter((item) => pickString(item, ["role"], "") === "alpha").length;
  const scoreFactors = operationalCatalogItems.filter((item) => pickBoolean(item, ["used_in_score"], false)).length;
  const governanceRuns = pickArray(evolutionQuery.data, ["items"]);
  const templateLogs = pickArray(templateLogsQuery.data, ["items"]);
  const activeTemplates = pickArray(activeTemplatesQuery.data, ["items"]);
  const learningApplications = factHasDisplayValue(learningApplicationsFact)
    ? pickArray(learningApplicationsQuery.data, ["applications", "items", "rows"])
    : [];
  const learningSamples = factHasDisplayValue(learningSamplesFact)
    ? pickArray(learningSamplesQuery.data, ["samples", "items", "rows"])
    : [];

  const replayKeys = [
    "bar_replay",
    "factor_frame_replay",
    "execution_gate_recompute",
    "risk_policy_recompute",
    "order_lifecycle_replay",
    "order_outcome_causality_replay",
    "broker_fill_slippage_replay",
    "supervisor_counterfactual_replay",
    "risk_policy_subaction_replay",
  ];

  return (
    <section className="dashboard v15-dashboard">
      <div className="dashboard-header">
        <div>
          <div className="eyebrow">自治运行平台</div>
          <h1>运行中枢</h1>
          <p>统一查看运行态、回放证据、自治健康、事故控制和发布审计。</p>
        </div>
        <div className="header-status">
          <StatusPill
            status={`${readyForFrontend ? "后端就绪" : "后端受限"}${readinessKnown ? "" : "（待确认）"}`}
            tone={factBoundTone(readinessFact, readyForFrontend ? "ok" : "warn", readinessRequestFailed)}
          />
          <StatusPill
            status={`基础证据 ${phase0Complete ? "已完成" : "待补"}${phase0Known ? "" : "（待确认）"}`}
            tone={factBoundTone(phase0Fact, phase0Complete ? "ok" : "warn", phase0RequestFailed)}
          />
          <StatusPill status="端点事实按来源独立校验" tone="warn" />
          <button className="header-refresh" type="button" onClick={refreshAll}>
            <RefreshCw size={15} />
            刷新
          </button>
        </div>
      </div>

      <div className="stat-grid v15-stat-grid">
        <StatTile icon={Gauge} label="自治健康" value={`${formatDecimal(healthScore, 1)}%`} detail={`${translateDisplayValue(healthPosture)}${readinessKnown ? "" : " · 保留值"}`} tone={factBoundTone(readinessFact, healthPosture === "full" ? "ok" : healthPosture === "frozen" ? "bad" : "warn", readinessRequestFailed)} />
        <StatTile icon={Workflow} label="回放证据" value={latestReplayGrade} detail={`误差 ${formatDecimal(replayMismatch, 0)}${replayError ? " · 有异常" : ""}`} tone={factBoundTone(replayDisplayFact, latestReplayGrade === "A" || latestReplayGrade === "B" ? "ok" : latestReplayGrade === "C" ? "warn" : "bad", replayDisplayRequestFailed)} />
        <StatTile icon={Siren} label="事故模式" value={translateDisplayValue(incidentMode)} detail={`阻断项 ${formatDecimal(blockers.length, 0)}`} tone={factBoundTone(incidentControlFact, incidentMode === "normal" ? "ok" : "warn", incidentControlRequestFailed)} />
        <StatTile icon={Rocket} label="发布审计" value={translateDisplayValue(releaseStatus)} detail={phase0Known ? (operationallyReady ? "现场证据已齐" : "待补现场证据") : "现场证据待确认"} tone={factBoundTone(releaseFact, toneFromStatus(releaseStatus), releaseRequestFailed)} />
      </div>

      <div className="v15-tabbar" role="tablist" aria-label="运行中枢分区">
        {tabs.map((tab) => {
          const Icon = tab.icon;
          const active = activeTab === tab.key;
          return (
            <button key={tab.key} className={`v15-tab ${active ? "v15-tab-active" : ""}`} type="button" role="tab" aria-selected={active} onClick={() => setActiveTab(tab.key)}>
              <Icon size={15} aria-hidden="true" />
              <span>{tab.label}</span>
            </button>
          );
        })}
      </div>

      {activeTab === "runtime" ? (
        <div className="dashboard-grid">
          <MetricCard title="运行态合约" className="wide-panel">
            <div className="v15-mini-grid">
              <CompactMetric label="就绪状态" value={readyForFrontend ? "就绪" : "受限"} detail={`${pickString(readiness, ["schema_version"], "")}${readinessKnown ? "" : " / 保留值"}`} tone={factBoundTone(readinessFact, boolTone(readyForFrontend), readinessRequestFailed)} />
              <CompactMetric label="运行覆盖层" value={safeLabel(pick(v15, ["overlay.status", "snapshot.status"]))} detail={safeLabel(pick(v15, ["snapshot.config_hash", "snapshot.overlay_hash"]))} tone={factBoundTone(readinessFact, toneFromStatus(safeLabel(pick(v15, ["overlay.status", "snapshot.status"]))), readinessRequestFailed)} />
              <CompactMetric label="回滚快照" value={pickBoolean(v15, ["snapshot.ok"], false) ? "正常" : "缺失"} detail={formatTime(pick(v15, ["snapshot.created_at", "snapshot.updated_at"]))} tone={factBoundTone(readinessFact, pickBoolean(v15, ["snapshot.ok"], false) ? "ok" : "warn", readinessRequestFailed)} />
              <CompactMetric label="基础证据" value={phase0Complete ? "已完成" : "未完成"} detail={phase0Known ? (operationallyReady ? "可运行" : "待补证据") : "事实待确认"} tone={factBoundTone(phase0Fact, phase0Complete ? "ok" : "warn", phase0RequestFailed)} />
            </div>
            <div className="v15-two-col">
              <div>
                <SectionHead title="控制平面边界" status="已保护" tone={factBoundTone(readinessFact, "ok", readinessRequestFailed)} />
                <div className="field-list">
                  <Field label="RiskPolicyService" value={pickBoolean(v15, ["control_plane_boundaries.risk_policy_service_required"], false) ? "必须经过" : "缺失"} tone={factBoundTone(readinessFact, pickBoolean(v15, ["control_plane_boundaries.risk_policy_service_required"], false) ? "ok" : "bad", readinessRequestFailed)} />
                  <Field label="DecisionPolicy" value={pickBoolean(v15, ["control_plane_boundaries.decision_policy_required_for_weight_writes"], false) ? "权重写入必经" : "缺失"} tone={factBoundTone(readinessFact, pickBoolean(v15, ["control_plane_boundaries.decision_policy_required_for_weight_writes"], false) ? "ok" : "bad", readinessRequestFailed)} />
                  <Field label="覆盖层事实源" value={pickBoolean(v15, ["control_plane_boundaries.runtime_overlay_is_source_of_truth"], false) ? "数据库覆盖层" : "未知"} />
                  <Field label="快照回滚" value={pickBoolean(v15, ["control_plane_boundaries.runtime_snapshot_required_for_rollback"], false) ? "必须保留" : "缺失"} tone={factBoundTone(readinessFact, pickBoolean(v15, ["control_plane_boundaries.runtime_snapshot_required_for_rollback"], false) ? "ok" : "bad", readinessRequestFailed)} />
                </div>
              </div>
              <div>
                <SectionHead title="就绪阻断项" status={`${blockers.length}`} tone={factBoundTone(readinessFact, blockers.length ? "warn" : "ok", readinessRequestFailed)} />
                <DataList items={blockers} empty="无阻断项" fact={readinessFact} requestFailed={readinessRequestFailed} />
              </div>
            </div>
          </MetricCard>
        </div>
      ) : null}

      {activeTab === "factors" ? (
        <div className="dashboard-grid">
          <MetricCard title="因子目录" className="wide-panel">
            <div className="v15-mini-grid">
              <CompactMetric label="运行条目" value={formatDecimal(operationalCount, 0)} detail={`${catalogSourceLabel} / 候选全集 ${formatDecimal(catalogCount, 0)}`} tone={factBoundTone(catalogDisplayFact, operationalCount ? "ok" : "warn", catalogDisplayRequestFailed)} />
              <CompactMetric label="Alpha 因子" value={formatDecimal(alphaFactors, 0)} detail={`参与打分 ${formatDecimal(scoreFactors, 0)}`} tone={factBoundTone(catalogDisplayFact, alphaFactors ? "ok" : "warn", catalogDisplayRequestFailed)} />
              <CompactMetric label="目录快照" value={pickBoolean(snapshotCatalog, ["ok"], false) ? "可用" : safeLabel(pick(snapshotCatalog, ["status"]))} detail={formatTime(pick(snapshotCatalog, ["created_at", "updated_at"]))} tone={factBoundTone(catalogSnapshotFact, pickBoolean(snapshotCatalog, ["ok"], false) ? "ok" : "warn", catalogSnapshotRequestFailed)} />
            </div>
            <FactorCatalogList items={operationalCatalogItems} fact={catalogDisplayFact} requestFailed={catalogDisplayRequestFailed} />
          </MetricCard>
        </div>
      ) : null}

      {activeTab === "governance" ? (
        <div className="dashboard-grid">
          <MetricCard title="治理运行">
            <GovernanceRunList items={governanceRuns} />
          </MetricCard>
          <MetricCard title="参数模板">
            <div className="v15-mini-grid v15-mini-grid-tight">
              <CompactMetric label="当前模板" value={formatDecimal(activeTemplates.length, 0)} tone={factBoundTone(activeTemplatesFact, activeTemplates.length ? "ok" : "mute", activeTemplatesRequestFailed)} />
              <CompactMetric label="切换记录" value={formatDecimal(templateLogs.length, 0)} tone={unverifiedTone(templateLogs.length ? "ok" : "mute")} />
            </div>
            <TemplateList
              items={templateLogs.length ? templateLogs : activeTemplates}
              fact={templateLogs.length ? undefined : activeTemplatesFact}
              requestFailed={templateLogs.length ? templateLogsQuery.isError || templateLogsQuery.isRefetchError : activeTemplatesRequestFailed}
            />
          </MetricCard>
        </div>
      ) : null}

      {activeTab === "replay" ? (
        <div className="dashboard-grid">
          <MetricCard title="K线回放" className="wide-panel">
            <div className="v15-action-row">
              <ReplayDecisionPicker items={replayChoices} value={selectedReplayDecisionId} onChange={setSelectedReplayDecisionId} />
              <ActionButton icon={Play} label="生成回放窗口" variant="primary" loading={runReplayMutation.isPending} error={runReplayMutation.isError ? "回放请求失败" : null} onAction={() => runAndDiscard(runReplayMutation.mutateAsync(selectedReplayDecisionId))} />
            </div>
            <div className="v15-replay-status">
              {runReplayMutation.isPending ? <StatusPill status="正在生成回放窗口" tone="warn" /> : null}
              {runReplayMutation.isSuccess ? <StatusPill status="回放请求已完成（事实未验证）" tone="warn" /> : null}
              {runReplayMutation.isError ? <StatusPill status="回放失败" tone="bad" /> : null}
              {replayChoicesRequestFailed ? <StatusPill status="历史候选读取异常" tone="bad" /> : null}
              <span>本次显示1个已选择决策窗口 / 前40根 + 后24根K线</span>
              <span>历史候选 {formatDecimal(replayChoices.length, 0)} 条</span>
              <span>最近更新时间 {formatTime(pick(replayDisplayReport, ["created_at"]))}</span>
            </div>
            <KlineWindowPreview preview={barWindowPreview} pending={runReplayMutation.isPending} />
            <TradeOutcomeLearningPanel preview={tradeOutcomeLearningPreview} fact={replayDisplayFact} requestFailed={replayDisplayRequestFailed} />
            <div className="v15-mini-grid">
              <CompactMetric label="回放编号" value={pickString(replayDisplayReport, ["replay_run_id"], "")} detail={formatTime(pick(replayDisplayReport, ["created_at"]))} tone={factBoundTone(replayDisplayFact, latestReplayGrade === "missing" ? "warn" : "ok", replayDisplayRequestFailed)} />
              <CompactMetric label="证据等级" value={latestReplayGrade} detail={`误差 ${formatDecimal(replayMismatch, 0)}`} tone={factBoundTone(replayDisplayFact, latestReplayGrade === "A" || latestReplayGrade === "B" ? "ok" : latestReplayGrade === "C" ? "warn" : "bad", replayDisplayRequestFailed)} />
              <CompactMetric label="决策数量" value={formatDecimal(pickNumber(replayDisplayReport, ["decision_count"], 0), 0)} detail={`匹配 ${formatDecimal(pickNumber(replayDisplayReport, ["matched_live_count"], 0), 0)}`} tone="mute" />
              <CompactMetric label="证据文件" value={pickString(replayDisplayReport, ["artifact_hash"], "").slice(0, 12) || ""} detail={pickString(replayDisplayReport, ["artifact_path"], "")} tone={factBoundTone(replayDisplayFact, pickString(replayDisplayReport, ["artifact_hash"], "") ? "ok" : "warn", replayDisplayRequestFailed)} />
              <CompactMetric label="K线覆盖" value={`${formatDecimal(scorePct(pickNumber(barReplayMetrics, ["bar_window_coverage"], 0)), 1)}%`} detail={`缺口 ${formatDecimal(pickNumber(barReplayMetrics, ["missing_bar_window_count"], 0), 0)} / 过期 ${formatDecimal(pickNumber(barReplayMetrics, ["stale_bar_alignment_count"], 0), 0)}`} tone={factBoundTone(replayDisplayFact, scorePct(pickNumber(barReplayMetrics, ["bar_window_coverage"], 0)) >= 95 ? "ok" : "warn", replayDisplayRequestFailed)} />
              <CompactMetric label="K线窗口Hash" value={pickString(barReplayMetrics, ["bar_window_hash"], "").slice(0, 12) || ""} detail={`前 ${formatDecimal(pickNumber(barReplayMetrics, ["warmup_bars"], 0), 0)} / 后 ${formatDecimal(pickNumber(barReplayMetrics, ["post_bars"], 0), 0)}`} tone={factBoundTone(replayDisplayFact, pickString(barReplayMetrics, ["bar_window_hash"], "") ? "ok" : "warn", replayDisplayRequestFailed)} />
            </div>
            <SectionHead title="K线窗口严重缺口" status={`${barWindowIssues(pickArray(barReplayMetrics, ["mismatch_examples"]), "blocking").length}`} tone={factBoundTone(replayDisplayFact, barWindowIssues(pickArray(barReplayMetrics, ["mismatch_examples"]), "blocking").length ? "warn" : "ok", replayDisplayRequestFailed)} />
            <BarWindowIssueList items={barWindowIssues(pickArray(barReplayMetrics, ["mismatch_examples"]), "blocking")} />
            <SectionHead title="预热窗口提示" status={`${barWindowIssues(pickArray(barReplayMetrics, ["mismatch_examples"]), "notice").length}`} tone={factBoundTone(replayDisplayFact, barWindowIssues(pickArray(barReplayMetrics, ["mismatch_examples"]), "notice").length ? "warn" : "ok", replayDisplayRequestFailed)} />
            <BarWindowIssueList items={barWindowIssues(pickArray(barReplayMetrics, ["mismatch_examples"]), "notice")} />
            {pickString(replayDisplayReport, ["scope.kind"], "") === "bar_replay_evidence" ? (
              <div className="v15-progress-grid">
                {replayKeys.map((key) => {
                  const metric = asRecord(replayMetrics[key]);
                  const coverage = scorePct(pickNumber(metric, ["coverage", "bar_window_coverage", "factor_frame_coverage", "causality_coverage", "counterfactual_coverage", "agreement_rate"], 0));
                  return <ProgressMetric key={key} label={replayMetricLabels[key] || key} value={coverage} detail={pickString(metric, ["schema_version"], "")} tone={factBoundTone(replayDisplayFact, coverage >= 95 ? "ok" : coverage > 0 ? "warn" : "mute", replayDisplayRequestFailed)} />;
                })}
              </div>
            ) : null}
          </MetricCard>
        </div>
      ) : null}

      {activeTab === "risk" ? (
        <div className="dashboard-grid">
          <MetricCard title="风控与事故边界">
            <div className="field-list">
              <Field label="事故模式" value={translateDisplayValue(incidentMode)} tone={factBoundTone(incidentControlFact, incidentMode === "normal" ? "ok" : "warn", incidentControlRequestFailed)} />
              <Field label="风控摘要" value={`${safeLabel(pick(risk, ["status", "overall", "system_health.status"]))}${riskKnown ? "" : "（待确认）"}`} tone={factBoundTone(riskFact, toneFromStatus(safeLabel(pick(risk, ["status", "overall", "system_health.status"]))), riskRequestFailed)} />
              <Field label="运行闸门" value={pickBoolean(v15, ["control_plane_boundaries.risk_policy_service_required"], false) ? "RiskPolicyService" : "未知"} tone={factBoundTone(readinessFact, pickBoolean(v15, ["control_plane_boundaries.risk_policy_service_required"], false) ? "ok" : "bad", readinessRequestFailed)} />
            </div>
          </MetricCard>
          <MetricCard title="事故模式操作">
            <div className="v15-action-grid">
              {["shadow_only", "no_new_risk", "only_close", "frozen", "normal"].map((mode) => (
                <ActionButton key={mode} icon={SlidersHorizontal} label={translateDisplayValue(mode)} variant={mode === "normal" ? "ghost" : mode === "frozen" ? "danger" : "primary"} loading={incidentModeMutation.isPending} disabled={mode === "normal" && !incidentControlKnown} error={incidentModeMutation.isError ? "切换失败" : null} onAction={() => runAndDiscard(incidentModeMutation.mutateAsync(mode))} />
              ))}
            </div>
          </MetricCard>
        </div>
      ) : null}

      {activeTab === "learning" ? (
        <div className="dashboard-grid">
          <MetricCard title="学习健康">
            <div className="v15-mini-grid v15-mini-grid-tight">
              <CompactMetric label="应用记录" value={formatDecimal(learningApplications.length, 0)} tone={factBoundTone(learningApplicationsFact, learningApplications.length ? "ok" : "mute", learningApplicationsRequestFailed)} />
              <CompactMetric label="学习样本" value={formatDecimal(learningSamples.length, 0)} tone={factBoundTone(learningSamplesFact, learningSamples.length ? "ok" : "mute", learningSamplesRequestFailed)} />
              <CompactMetric label="摘要状态" value={safeLabel(pick(learningSummary, ["status", "schema_version"]))} tone={factBoundTone(learningSummaryFact, toneFromStatus(safeLabel(pick(learningSummary, ["status"]))), learningSummaryRequestFailed)} />
            </div>
            <DataList items={learningApplications} empty="暂无学习应用记录" fact={learningApplicationsFact} requestFailed={learningApplicationsRequestFailed} />
          </MetricCard>
          <MetricCard title="自治学习样本">
            <DataList items={learningSamples} empty="暂无自治样本" fact={learningSamplesFact} requestFailed={learningSamplesRequestFailed} />
          </MetricCard>
        </div>
      ) : null}

      {activeTab === "incidents" ? (
        <div className="dashboard-grid">
          <MetricCard title="事故控制">
            <div className="v15-action-row">
              <ActionButton icon={ClipboardList} label="生成应急预案" variant="primary" loading={playbookMutation.isPending} error={playbookMutation.isError ? "生成失败" : null} onAction={() => runAndDiscard(playbookMutation.mutateAsync())} />
              <ActionButton icon={CheckCircle2} label="执行健康收紧" variant="danger" loading={enforceScopeMutation.isPending} error={enforceScopeMutation.isError ? "执行失败" : null} onAction={() => runAndDiscard(enforceScopeMutation.mutateAsync())} />
            </div>
            <div className="field-list">
              <Field label="当前模式" value={incidentMode} tone={factBoundTone(incidentControlFact, incidentMode === "normal" ? "ok" : "warn", incidentControlRequestFailed)} />
              <Field label="应急预案" value={pickString(latestPlaybook, ["playbook_id", "status"], "")} />
              <Field label="目标模式" value={pickString(latestPlaybook, ["target_mode"], "")} tone={factBoundTone(incidentPlaybookFact, toneFromStatus(pickString(latestPlaybook, ["target_mode"], "")), incidentPlaybookRequestFailed)} />
              <Field label="范围审批" value={pickString(latestScopeApproval, ["decision", "status"], "")} tone={factBoundTone(scopeApprovalFact, toneFromStatus(pickString(latestScopeApproval, ["decision", "status"], "")), scopeApprovalRequestFailed)} />
              <Field label="范围收紧" value={pickString(latestScopeEnforcement, ["status"], "")} tone={factBoundTone(scopeEnforcementFact, toneFromStatus(pickString(latestScopeEnforcement, ["status"], "")), scopeEnforcementRequestFailed)} />
            </div>
          </MetricCard>
          <MetricCard title="最近应急预案">
            <JsonBlock value={latestPlaybook} />
          </MetricCard>
        </div>
      ) : null}

      {activeTab === "release" ? (
        <div className="dashboard-grid">
          <MetricCard title="发布纪律">
            <div className="v15-action-row">
              <ActionButton icon={Rocket} label="开始发布记录" variant="primary" loading={releaseStartMutation.isPending} error={releaseStartMutation.isError ? "启动失败" : null} onAction={() => runAndDiscard(releaseStartMutation.mutateAsync())} />
            </div>
            <div className="field-list">
              <Field label="发布编号" value={releaseRunId || ""} />
              <Field label="状态" value={releaseStatus} tone={factBoundTone(releaseFact, toneFromStatus(releaseStatus), releaseRequestFailed)} />
              <Field label="回放编号" value={pickString(release, ["replay_run_id"], "")} />
              <Field label="运行配置哈希" value={pickString(release, ["runtime_config_hash"], "")} />
              <Field label="就绪姿态" value={pickString(release, ["readiness_posture"], "")} />
            </div>
          </MetricCard>
          <MetricCard title="审批轨迹">
            <DataList items={releaseApprovals} empty="暂无发布审批事件" fact={releaseApprovalsFact} requestFailed={releaseApprovalsRequestFailed} />
          </MetricCard>
        </div>
      ) : null}
    </section>
  );
}

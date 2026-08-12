export type FactState = "known" | "unknown" | "stale" | "error";

export type FactEnvelope = {
  envelope: "fact.v1";
  contract: string;
  state: FactState;
  source: string;
  observed_at: string | number | null;
  generated_at: string | number | null;
  stale_after_sec: number;
  reason_code: string | null;
  components?: Record<string, unknown>;
};

export type FactCarrier = { _fact?: Partial<FactEnvelope> };

const validStates = new Set<FactState>(["known", "unknown", "stale", "error"]);
const unavailableSources = new Set([
  "",
  "none",
  "unknown",
  "unavailable",
  "not_registered",
  "degraded_cache",
]);

function epochSeconds(value: unknown): number {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value > 1e12 ? value / 1000 : value;
  }
  if (typeof value === "string" && value.trim()) {
    const numeric = Number(value);
    if (Number.isFinite(numeric)) return numeric > 1e12 ? numeric / 1000 : numeric;
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed / 1000 : 0;
  }
  return 0;
}

export function factAgeSeconds(fact: FactEnvelope, now = Date.now() / 1000): number | null {
  const observedAt = epochSeconds(fact.observed_at);
  if (observedAt <= 0 || !Number.isFinite(now)) return null;
  return Math.max(0, now - observedAt);
}

function unknownFact(expectedContract: string, reasonCode: string): FactEnvelope {
  return {
    envelope: "fact.v1",
    contract: expectedContract,
    state: "unknown",
    source: "none",
    observed_at: null,
    generated_at: null,
    stale_after_sec: 0,
    reason_code: reasonCode,
    components: {},
  };
}

function normalizeFact(raw: Partial<FactEnvelope> | undefined, expectedContract: string, missingReason: string): FactEnvelope {
  if (!raw || raw.envelope !== "fact.v1") {
    return unknownFact(expectedContract, missingReason);
  }
  const declaredContract = String(raw.contract || "");
  if (expectedContract && declaredContract !== expectedContract) {
    return {
      envelope: "fact.v1",
      contract: expectedContract,
      state: "unknown",
      source: "none",
      observed_at: raw.observed_at ?? null,
      generated_at: raw.generated_at ?? null,
      stale_after_sec: Math.max(0, Number(raw.stale_after_sec || 0)),
      reason_code: "fact_contract_mismatch",
      components: {},
    };
  }
  const declaredState = validStates.has(raw.state as FactState) ? raw.state as FactState : "unknown";
  const observedAt = epochSeconds(raw.observed_at);
  const staleAfter = Math.max(0, Number(raw.stale_after_sec || 0));
  const age = observedAt > 0 ? Date.now() / 1000 - observedAt : Number.POSITIVE_INFINITY;
  const source = String(raw.source || "none");
  let computedState: FactState = declaredState;
  let reasonCode = raw.reason_code ? String(raw.reason_code) : null;
  if (declaredState !== "error" && unavailableSources.has(source.trim().toLowerCase())) {
    computedState = "unknown";
    reasonCode = reasonCode || "fact_source_unavailable";
  } else if ((declaredState === "known" || declaredState === "stale") && observedAt <= 0) {
    computedState = "unknown";
    reasonCode = reasonCode || "fact_observation_missing";
  } else if (declaredState === "known" && staleAfter > 0 && age > staleAfter) {
    computedState = "stale";
    reasonCode = reasonCode || "fact_freshness_expired";
  }
  return {
    envelope: "fact.v1",
    contract: declaredContract || expectedContract,
    state: computedState,
    source,
    observed_at: raw.observed_at ?? null,
    generated_at: raw.generated_at ?? null,
    stale_after_sec: staleAfter,
    reason_code: reasonCode,
    components: raw.components && typeof raw.components === "object" ? raw.components : {},
  };
}

export function readFact(payload: unknown, expectedContract = ""): FactEnvelope {
  const raw = payload && typeof payload === "object"
    ? (payload as FactCarrier)._fact
    : undefined;
  return normalizeFact(raw, expectedContract, "missing_fact_envelope");
}

export function readFactComponent(
  payload: unknown,
  componentName: string,
  expectedContract = "",
): FactEnvelope {
  const parent = payload && typeof payload === "object"
    ? (payload as FactCarrier)._fact
    : undefined;
  const component = parent?.components && typeof parent.components === "object"
    ? parent.components[componentName]
    : undefined;
  const raw = component && typeof component === "object"
    ? component as Partial<FactEnvelope>
    : undefined;
  return normalizeFact(raw, expectedContract, "missing_fact_component");
}

/** Read a declared component path without recursively guessing field names. */
export function readFactNestedComponent(
  payload: unknown,
  componentPath: readonly string[],
  expectedContract = "",
): FactEnvelope {
  const parent = payload && typeof payload === "object"
    ? (payload as FactCarrier)._fact
    : undefined;
  let current: unknown = parent;
  for (const name of componentPath) {
    if (!current || typeof current !== "object" || Array.isArray(current)) {
      current = undefined;
      break;
    }
    const record = current as Record<string, unknown>;
    const components = record.components;
    current = components && typeof components === "object" && !Array.isArray(components)
      ? (components as Record<string, unknown>)[name]
      : record[name];
  }
  const raw = current && typeof current === "object" && !Array.isArray(current)
    ? current as Partial<FactEnvelope>
    : undefined;
  return normalizeFact(raw, expectedContract, "missing_fact_component");
}

export function mergeFactRecord<T extends Record<string, unknown>>(
  endpointRecord: T,
  snapshotRecord: Record<string, unknown>,
  endpointFact: FactEnvelope,
  snapshotFact: FactEnvelope,
): T {
  if (factHasDisplayValue(endpointFact)) {
    return {
      ...(factHasDisplayValue(snapshotFact) ? snapshotRecord : {}),
      ...endpointRecord,
    } as T;
  }
  if (factHasDisplayValue(snapshotFact)) {
    return { ...snapshotRecord } as T;
  }
  return {} as T;
}

export function factAllowsNewRisk(...facts: FactEnvelope[]): boolean {
  return facts.length > 0 && facts.every((fact) => fact.state === "known");
}

export function factIsKnown(fact: FactEnvelope, requestFailed = false): boolean {
  return !requestFailed && fact.state === "known";
}

export type FactBoundTone = "ok" | "warn" | "bad" | "mute" | "pending" | "stale";

/**
 * UI freshness is deliberately separate from the endpoint's business value.
 * A cached known value with a failed refresh is not a current fact. Pages use
 * this helper as the single display boundary so stale/error payloads cannot
 * leak old business values into the console.
 */
export type FactViewState = "known" | "stale" | "pending" | "error";

export function factViewState(fact: FactEnvelope, requestFailed = false): FactViewState {
  if (requestFailed || fact.state === "error") return "error";
  if (fact.state === "stale") return "stale";
  if (fact.state === "known") return "known";
  return "pending";
}

export type FactViewEntry = {
  fact: FactEnvelope;
  requestFailed?: boolean;
};

export function aggregateFactViewState(entries: readonly FactViewEntry[]): FactViewState {
  if (entries.some(({ fact, requestFailed }) => factViewState(fact, requestFailed) === "error")) return "error";
  if (entries.some(({ fact, requestFailed }) => factViewState(fact, requestFailed) === "stale")) return "stale";
  if (entries.some(({ fact, requestFailed }) => factViewState(fact, requestFailed) === "pending")) return "pending";
  return "known";
}

export function factViewStateLabel(state: FactViewState): string {
  if (state === "known") return "已确认";
  if (state === "stale") return "数据已过期";
  if (state === "error") return "接口刷新失败";
  return "数据待确认";
}

export function factViewLabel(fact: FactEnvelope, requestFailed = false): string {
  const state = factViewState(fact, requestFailed);
  if (state === "error" && requestFailed) return "读取失败，暂无实时数据";
  if (state === "error") return "读取错误";
  return factViewStateLabel(state);
}

/**
 * A live control-plane value is displayable only while its authoritative fact
 * is known. Stale/unknown/error facts remain available for diagnostics, but
 * their old business value must not be rendered as current data.
 * React Query may keep the previous payload after a refetch failure, so request
 * failure is an explicit input instead of being inferred from the cached envelope.
 */
export function factBoundTone(
  fact: FactEnvelope,
  tone: FactBoundTone,
  requestFailed = false,
): FactBoundTone {
  if (requestFailed || fact.state === "error") return "bad";
  if (tone === "ok" && fact.state === "stale") return "stale";
  if (tone === "ok" && fact.state !== "known") return "pending";
  return tone;
}

export function factHasDisplayValue(fact: FactEnvelope, requestFailed = false): boolean {
  return !requestFailed && fact.state === "known";
}

export function factStatusLabel(fact: FactEnvelope, requestFailed = false): string {
  if (requestFailed) return "暂无实时数据";
  if (fact.state === "known") return "已确认";
  if (fact.state === "stale") return "已过期";
  if (fact.state === "error") return "读取错误";
  return "未知";
}

export const LIVE_AUTONOMY_STATUS_CONTRACT = "ops.live-autonomy-status.v2";
export const LIVE_AUTONOMY_EVALUATION_CONTRACT = "ops.live-autonomy-unlock-evaluation.v2";

type AnyRecord = Record<string, unknown>;

function directRecord(value: unknown): AnyRecord {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as AnyRecord
    : {};
}

function explicitBoolean(record: AnyRecord, key: string): boolean | null {
  return typeof record[key] === "boolean" ? record[key] as boolean : null;
}

export type LiveAutonomyEndpointView = {
  fact: FactEnvelope;
  liveAutonomy: AnyRecord;
  evaluation: AnyRecord;
  unlockAllowed: boolean;
};

function evaluationAllowsUnlock(evaluation: AnyRecord): boolean {
  return explicitBoolean(evaluation, "ok") === true
    && evaluation.status === "unlock_ready";
}

export function decodeLiveAutonomyStatus(
  payload: unknown,
  requestFailed = false,
): LiveAutonomyEndpointView {
  const endpoint = directRecord(payload);
  const liveAutonomy = directRecord(endpoint.live_autonomy);
  const evaluation = directRecord(liveAutonomy.evaluation);
  const fact = readFact(endpoint, LIVE_AUTONOMY_STATUS_CONTRACT);
  return {
    fact,
    liveAutonomy,
    evaluation,
    unlockAllowed: factIsKnown(fact, requestFailed) && evaluationAllowsUnlock(evaluation),
  };
}

export function decodeLiveAutonomyEvaluation(
  payload: unknown,
  requestFailed = false,
): LiveAutonomyEndpointView {
  const endpoint = directRecord(payload);
  const evaluation = directRecord(endpoint.evaluation);
  const fact = readFact(endpoint, LIVE_AUTONOMY_EVALUATION_CONTRACT);
  return {
    fact,
    liveAutonomy: {},
    evaluation,
    unlockAllowed: factIsKnown(fact, requestFailed) && evaluationAllowsUnlock(evaluation),
  };
}

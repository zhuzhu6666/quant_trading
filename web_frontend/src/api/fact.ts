import { epochSeconds, serverNowSeconds } from "./time.ts";

export type FactState = "known" | "unknown" | "stale" | "error";

export type FactComponent = {
  envelope?: "fact.v1";
  contract?: string;
  state?: FactState;
  source?: string;
  observed_at?: string | number | null;
  generated_at?: string | number | null;
  stale_after_sec?: number;
  reason_code?: string | null;
};

export type FactComponents = {
  account?: FactComponent;
  broker?: FactComponent;
  identity?: FactComponent;
  loop?: FactComponent;
  pnl?: FactComponent;
  positions?: FactComponent;
  price?: FactComponent;
  protection?: FactComponent;
  readiness?: FactComponent;
  risk?: FactComponent;
  session?: FactComponent;
  [componentName: string]: FactComponent | undefined;
};

export type FactEnvelope = {
  envelope: "fact.v1";
  contract: string;
  state: FactState;
  source: string;
  observed_at: string | number | null;
  generated_at: string | number | null;
  stale_after_sec: number;
  reason_code: string | null;
  components?: FactComponents;
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

export function factStateFromRaw(value: unknown): FactState {
  return typeof value === "string" && validStates.has(value as FactState)
    ? value as FactState
    : "unknown";
}

export function factAgeSeconds(fact: FactEnvelope, now = serverNowSeconds()): number | null {
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

function normalizeFact(
  raw: Partial<FactEnvelope> | undefined,
  expectedContract: string,
  missingReason: string,
): FactEnvelope {
  if (!raw || raw.envelope !== "fact.v1") {
    return unknownFact(expectedContract, missingReason);
  }
  const declaredContract = typeof raw.contract === "string" ? raw.contract : "";
  if (expectedContract && declaredContract !== expectedContract) {
    return {
      envelope: "fact.v1",
      contract: expectedContract,
      state: "unknown",
      source: "none",
      observed_at: raw.observed_at ?? null,
      generated_at: raw.generated_at ?? null,
      stale_after_sec: Math.max(0, Number(raw.stale_after_sec ?? 0)),
      reason_code: "fact_contract_mismatch",
      components: {},
    };
  }
  const declaredState = factStateFromRaw(raw.state);
  const observedAt = epochSeconds(raw.observed_at);
  const staleAfter = Math.max(0, Number(raw.stale_after_sec ?? 0));
  const source = typeof raw.source === "string" && raw.source.trim() ? raw.source : "none";
  let state = declaredState;
  let reasonCode = typeof raw.reason_code === "string" ? raw.reason_code : null;
  if (declaredState !== "error" && unavailableSources.has(source.trim().toLowerCase())) {
    state = "unknown";
    reasonCode = reasonCode || "fact_source_unavailable";
  } else if ((declaredState === "known" || declaredState === "stale") && observedAt <= 0) {
    state = "unknown";
    reasonCode = reasonCode || "fact_observation_missing";
  }
  return {
    envelope: "fact.v1",
    contract: declaredContract || expectedContract,
    state,
    source,
    observed_at: raw.observed_at ?? null,
    generated_at: raw.generated_at ?? null,
    stale_after_sec: staleAfter,
    reason_code: reasonCode,
    components: raw.components,
  };
}

export function readFact(payload: unknown, expectedContract = ""): FactEnvelope {
  const raw = payload && typeof payload === "object" ? (payload as FactCarrier)._fact : undefined;
  return normalizeFact(raw, expectedContract, "missing_fact_envelope");
}

/** Read one declared fact component. Missing components remain unknown. */
export function readFactComponent(
  payload: unknown,
  componentName: keyof FactComponents | string,
  expectedContract = "",
): FactEnvelope {
  const parent = payload && typeof payload === "object" ? (payload as FactCarrier)._fact : undefined;
  const component = parent?.components?.[componentName];
  return normalizeFact(component, expectedContract, "missing_fact_component");
}

export function factIsKnown(fact: FactEnvelope, requestFailed = false): boolean {
  return !requestFailed && fact.state === "known";
}

export type FactViewState = FactState;

export function factViewState(fact: FactEnvelope, requestFailed = false): FactViewState {
  if (requestFailed || fact.state === "error") return "error";
  if (fact.state === "stale") return "stale";
  if (fact.state === "known") return "known";
  return "unknown";
}

export type FactViewEntry = {
  fact: FactEnvelope;
  requestFailed?: boolean;
};

export function aggregateFactViewState(entries: readonly FactViewEntry[]): FactViewState {
  if (entries.some(({ fact, requestFailed }) => factViewState(fact, requestFailed) === "error")) return "error";
  if (entries.some(({ fact, requestFailed }) => factViewState(fact, requestFailed) === "stale")) return "stale";
  if (entries.some(({ fact, requestFailed }) => factViewState(fact, requestFailed) === "unknown")) return "unknown";
  return "known";
}

export function factViewStateLabel(state: FactViewState): string {
  if (state === "known") return "已确认";
  if (state === "stale") return "数据已过期";
  if (state === "error") return "接口刷新失败";
  return "数据未知";
}

export function factStatusLabel(fact: FactEnvelope, requestFailed = false): string {
  const state = factViewState(fact, requestFailed);
  if (state === "known") return "已确认";
  if (state === "stale") return "已过期";
  if (state === "error") return "读取错误";
  return "未知";
}

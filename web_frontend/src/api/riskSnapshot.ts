type JsonRecord = Record<string, unknown>;

function record(value: unknown): JsonRecord {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as JsonRecord
    : {};
}

function text(source: JsonRecord, key: string): string {
  const value = source[key];
  return typeof value === "string" ? value : "";
}

function finiteNumber(source: JsonRecord, key: string): number | null {
  const value = source[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export type ForwardVarView = {
  status: string;
  alpha: number | null;
  horizon: string;
  timeframe: string;
  sampleCount: number | null;
  currentEquity: number | null;
  currentNetNotionalUsd: number | null;
  varUsd: number | null;
  cvarUsd: number | null;
  varPct: number | null;
  cvarPct: number | null;
  symbol: string;
  sourceWindowStart: string;
  sourceWindowEnd: string;
  inputFingerprint: string;
};

function decodeForwardVar(source: JsonRecord): ForwardVarView {
  return {
    status: text(source, "status"),
    alpha: finiteNumber(source, "alpha"),
    horizon: text(source, "horizon"),
    timeframe: text(source, "timeframe"),
    sampleCount: finiteNumber(source, "sample_count"),
    currentEquity: finiteNumber(source, "current_equity"),
    currentNetNotionalUsd: finiteNumber(source, "current_net_notional_usd"),
    varUsd: finiteNumber(source, "var_usd"),
    cvarUsd: finiteNumber(source, "cvar_usd"),
    varPct: finiteNumber(source, "var_pct"),
    cvarPct: finiteNumber(source, "cvar_pct"),
    symbol: text(source, "symbol"),
    sourceWindowStart: text(source, "source_window_start"),
    sourceWindowEnd: text(source, "source_window_end"),
    inputFingerprint: text(source, "input_fingerprint"),
  };
}

export function decodeCanonicalRiskSnapshot(payload: unknown) {
  const root = record(payload);
  const snapshot = record(root.snapshot);
  const components = record(snapshot.components);
  const kelly = record(components.kelly);
  const stress = record(components.stress);
  const concentration = record(components.concentration);
  const schemaVersion = text(snapshot, "schema_version");

  return {
    contractKnown: schemaVersion === "risk_metrics_snapshot.v2",
    schemaVersion,
    status: text(snapshot, "status"),
    asOf: finiteNumber(snapshot, "as_of"),
    sampleCount: finiteNumber(snapshot, "sample_count"),
    sourceWindowStart: text(snapshot, "source_window_start"),
    sourceWindowEnd: text(snapshot, "source_window_end"),
    inputFingerprint: text(snapshot, "input_fingerprint"),
    var95: decodeForwardVar(record(components.var)),
    var99: decodeForwardVar(record(components.var_shadow_99)),
    kelly: {
      status: text(kelly, "status"),
      fraction: finiteNumber(kelly, "kelly_fraction"),
      halfKelly: finiteNumber(kelly, "half_kelly"),
      quarterKelly: finiteNumber(kelly, "quarter_kelly"),
      winRate: finiteNumber(kelly, "win_rate"),
      closedTrades: finiteNumber(kelly, "closed_trades"),
    },
    stress: {
      status: text(stress, "status"),
      lossUsd: finiteNumber(stress, "stress_loss_usd"),
      lossPct: finiteNumber(stress, "stress_loss_pct"),
      positionCount: finiteNumber(stress, "distinct_position_count"),
    },
    concentration: {
      status: text(concentration, "status"),
      fraction: finiteNumber(concentration, "concentration_fraction"),
      pct: finiteNumber(concentration, "concentration_pct"),
      maxSingleName: text(concentration, "max_single_name"),
      sampleCount: finiteNumber(concentration, "sample_count"),
      applicable: concentration.applicable === true,
      safe: concentration.is_safe === true,
    },
  };
}

export function knownMetric(status: string): boolean {
  return status === "known";
}

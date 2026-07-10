type AuthError = {
  message: string;
  status: number;
  detail?: unknown;
};

export type HttpMethod = "GET" | "POST" | "PUT" | "DELETE";

const envBase = typeof import.meta !== "undefined" && import.meta.env
  ? String(import.meta.env.VITE_API_BASE_URL || "")
  : "";
const API_BASE = envBase.replace(/\/$/, "");
let onUnauthorized: (() => void) | null = null;

export function setUnauthorizedHandler(handler: () => void): void {
  onUnauthorized = handler;
}

export function getApiBaseUrl(): string {
  return API_BASE || "";
}

function buildHttpUrl(path: string): string {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  return `${API_BASE}${normalizedPath}`;
}

function buildWsUrl(path: string): string {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  if (API_BASE && /^https?:\/\//i.test(API_BASE)) {
    const parsed = new URL(API_BASE);
    const scheme = parsed.protocol === "https:" ? "wss:" : "ws:";
    return `${scheme}//${parsed.host}${normalizedPath}`;
  }

  const defaultScheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${defaultScheme}//${window.location.host}${normalizedPath}`;
}

export function getWsUrl(): string {
  return buildWsUrl("/ws/state");
}

function parseResponse<T>(response: Response): Promise<T> {
  if (response.status === 204) {
    return Promise.resolve(null as T);
  }
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return response.json() as Promise<T>;
  }
  return response.text().then((text) => text as T);
}

export async function apiRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = localStorage.getItem("quant.auth.token");
  const headers = new Headers(init.headers || {});
  const hasBody = init.body !== undefined && init.body !== null;

  if (!headers.has("Content-Type") && hasBody) {
    headers.set("Content-Type", "application/json");
  }
  if (token && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  const response = await fetch(buildHttpUrl(path), {
    ...init,
    headers,
    credentials: "include",
  });

  if (response.status === 401) {
    const body = await parseResponse<unknown>(response);
    onUnauthorized?.();
    throw Object.assign(new Error("unauthorized"), {
      status: 401,
      detail: body,
      message: "Unauthorized",
    } satisfies AuthError);
  }

  if (!response.ok) {
    const body = await parseResponse<unknown>(response);
    throw Object.assign(new Error("request failed"), {
      status: response.status,
      detail: body,
      message: response.statusText,
    } satisfies AuthError);
  }

  return parseResponse<T>(response);
}

export const getJson = <T>(path: string, init: RequestInit = {}): Promise<T> =>
  apiRequest<T>(path, { ...init, method: "GET" });

export const postJson = <T>(path: string, body?: unknown, headers?: HeadersInit): Promise<T> =>
  apiRequest<T>(path, {
    method: "POST",
    headers: { ...headers },
    body: body ? JSON.stringify(body) : null,
  });

export type LoginPayload = {
  username: string;
  password: string;
};

export type LoginResponse = {
  user: string;
  token?: string;
  access_token?: string;
  token_type?: string;
  expires_in?: number;
};

export function extractLoginToken(response: LoginResponse): string {
  const raw = response.access_token || response.token || "";
  return raw.startsWith("Bearer ") ? raw.slice(7) : raw;
}

export type AuthMe = {
  user: string;
  authenticated: boolean;
};

export type HealthResponse = {
  status: string;
  db?: string;
  ctrader?: string;
  server_time?: string;
  uptime_seconds?: number;
  [key: string]: unknown;
};

export type LoopStatus = {
  running: boolean;
  broker: string;
  strategy: string;
  strategy_name?: string;
  strategyName?: string;
  started_at?: number | null;
  startedAt?: number | null;
  pid?: number | null;
  mode?: string;
  reason?: string;
  state?: string;
  [key: string]: unknown;
};

export type AccountPayload = {
  ok: boolean;
  broker?: string;
  balance?: number;
  equity?: number;
  margin?: number;
  margin_free?: number;
  free_margin?: number;
  leverage?: number;
  currency?: string;
  error?: string;
  warming_up?: boolean;
  readiness?: Record<string, unknown>;
  [key: string]: unknown;
};

export type PositionPayload = {
  ok: boolean;
  broker?: string;
  warming_up?: boolean;
  positions?: unknown[];
  error?: string;
  readiness?: Record<string, unknown>;
  [key: string]: unknown;
};

export type SessionStats = {
  pnl_today?: number;
  pnlToday?: number;
  trades?: number;
  win?: number;
  losses?: number;
  wins?: number;
  drawdown_pct?: number;
  drawdownPct?: number;
  drawdown?: number;
  consecutive_loss?: number;
  consecutiveLoss?: number;
  session_trades?: number;
  trade_count?: number;
};

export type RealizedPoint = {
  ts: number;
  cumulative: number;
  pnl: number;
  position_id?: number;
  deal_id?: number;
  source?: string;
  symbol?: string;
  direction?: number | string;
  gross?: number;
  swap?: number;
  commission?: number;
  [key: string]: unknown;
};

export type RealizedPnlSeries = {
  ok: boolean;
  scope: string;
  currency: string;
  source?: string;
  from_ts: number;
  to_ts: number;
  summary: {
    realized_pnl: number;
    trades: number;
    wins: number;
    losses: number;
    win_rate: number;
  };
  points: RealizedPoint[];
};

export type RiskSummary = {
  policy?: Record<string, unknown>;
  concentration?: Record<string, unknown>;
  var?: Record<string, unknown>;
  stress?: Record<string, unknown>;
  kelly?: Record<string, unknown>;
  system_health?: Record<string, unknown>;
  [key: string]: unknown;
};

export type DbHealthPayload = {
  updated_at?: number;
  databases: unknown[];
  errors?: string[];
  status?: string;
  ok?: boolean;
  overall?: string;
  checked_at?: number;
  summary?: {
    total?: number;
    fresh?: number;
    stale?: number;
    missing?: number;
  };
  [key: string]: unknown;
};

export type BackendReadinessPayload = {
  ok?: boolean;
  status?: string;
  summary?: Record<string, unknown>;
  ready_for_frontend?: boolean;
  readiness?: Record<string, unknown>;
  generated_at?: number;
  schema_version?: string;
  blockers?: unknown[];
  system_health?: Record<string, unknown>;
  service_health?: Record<string, unknown>;
  live?: Record<string, unknown>;
  backend_service?: Record<string, unknown>;
  high_load?: Record<string, unknown>;
  models?: Record<string, unknown>;
  factor_data?: Record<string, unknown>;
  [key: string]: unknown;
};

export type SystemLoadPayload = {
  ok?: boolean;
  ts?: number;
  cpu?: {
    percent?: number;
    load1?: number;
    load5?: number;
    load15?: number;
    cores?: number;
  };
  memory?: {
    total_bytes?: number;
    available_bytes?: number;
    used_bytes?: number;
    percent?: number;
  };
  disk?: {
    path?: string;
    total_bytes?: number;
    free_bytes?: number;
    used_bytes?: number;
    percent?: number;
  };
  process?: {
    pid?: number;
    rss_bytes?: number;
  };
  [key: string]: unknown;
};

export type LearningPayload = Record<string, unknown>;

export type StateSnapshot = Record<string, unknown> & {
  account?: Record<string, unknown>;
  positions_list?: unknown[];
  positions?: unknown[] | { positions?: unknown[]; ok?: boolean };
  loop_status?: Record<string, unknown>;
  session_stats?: SessionStats;
  daily?: Record<string, unknown>;
  closed_loop?: Record<string, unknown>;
  broker?: string;
  balance?: number;
  equity?: number;
  margin?: number;
  margin_free?: number;
  leverage?: number;
  currency?: string;
  active_strategy?: {
    id?: string;
    mode?: string;
    source?: string;
  };
  risk?: Record<string, unknown>;
  source?: string;
  session_pnl?: number;
  server_time?: string;
  consecutive_loss?: number;
};

export async function login(payload: LoginPayload): Promise<LoginResponse> {
  return postJson<LoginResponse>("/api/auth/login", payload);
}

export async function getAuthMe(): Promise<AuthMe> {
  return getJson<AuthMe>("/api/auth/me");
}

export async function getHealth(): Promise<HealthResponse> {
  return getJson<HealthResponse>("/api/health");
}

export async function getLoopStatus(): Promise<LoopStatus> {
  return getJson<LoopStatus>("/api/live/loop-status");
}

export async function getLiveStatus(): Promise<Record<string, unknown>> {
  return getJson<Record<string, unknown>>("/api/live/status");
}

export async function getStrategyStatus(): Promise<Record<string, unknown>> {
  return getJson<Record<string, unknown>>("/api/live/strategy-status");
}

export async function getAccount(): Promise<AccountPayload> {
  return getJson<AccountPayload>("/api/live/account");
}

export async function getPositions(): Promise<PositionPayload> {
  return getJson<PositionPayload>("/api/live/positions");
}

export async function getSessionStats(): Promise<SessionStats> {
  return getJson<SessionStats>("/api/live/session-stats");
}

export async function getRealizedPnlSeries(scope: string): Promise<RealizedPnlSeries> {
  const url = `/api/live/realized-pnl-series?scope=${encodeURIComponent(scope)}`;
  return getJson<RealizedPnlSeries>(url);
}

export async function getRiskSummary(): Promise<RiskSummary> {
  return getJson<RiskSummary>("/api/risk/summary");
}

export async function getRiskPolicyVerdicts(limit = 50): Promise<Record<string, unknown>> {
  return getJson<Record<string, unknown>>(`/api/risk/policy/verdicts?limit=${encodeURIComponent(String(limit))}`);
}

export async function getRecentTradeTraces(limit = 20): Promise<Record<string, unknown>> {
  return getJson<Record<string, unknown>>(`/api/risk/trade-trace/recent?limit=${encodeURIComponent(String(limit))}`);
}

export async function getSystemDbHealth(): Promise<DbHealthPayload> {
  return getJson<DbHealthPayload>("/api/system/db-health");
}

export async function getSystemLoad(): Promise<SystemLoadPayload> {
  return getJson<SystemLoadPayload>("/api/system/load");
}

export async function getLogTail(lines = 30): Promise<Record<string, unknown>> {
  return getJson<Record<string, unknown>>(`/api/logs/tail?lines=${encodeURIComponent(String(lines))}`);
}

export async function getOpsAlerts(): Promise<Record<string, unknown>> {
  return getJson<Record<string, unknown>>("/api/ops/alerts");
}

export async function getOpsRecovery(): Promise<Record<string, unknown>> {
  return getJson<Record<string, unknown>>("/api/ops/recovery");
}

export async function getSyncStatus(): Promise<Record<string, unknown>> {
  return getJson<Record<string, unknown>>("/api/sync/status");
}

export async function getCtraderTokenStatus(): Promise<Record<string, unknown>> {
  return getJson<Record<string, unknown>>("/api/ctrader/token-status");
}

export async function getExternalDataStatus(): Promise<Record<string, unknown>> {
  return getJson<Record<string, unknown>>("/api/data/external-status");
}

export async function getFactorV4Stats(): Promise<Record<string, unknown>> {
  return getJson<Record<string, unknown>>("/api/v4/stats");
}

export async function getFactorCatalog(snapshot = false): Promise<Record<string, unknown>> {
  const query = snapshot ? "?snapshot=latest" : "";
  return getJson<Record<string, unknown>>(`/api/v4/catalog${query}`);
}

export async function getFactorV4RecentTicks(): Promise<unknown[]> {
  return getJson<unknown[]>("/api/v4/recent-ticks");
}

export async function getLearningSummary(): Promise<LearningPayload> {
  return getJson<LearningPayload>("/api/learning/summary");
}

export async function getLearningSuggestions(limit = 20): Promise<LearningPayload> {
  return getJson<LearningPayload>(`/api/learning/suggestions?limit=${encodeURIComponent(String(limit))}`);
}

export async function getLearningApplications(limit = 20): Promise<LearningPayload> {
  return getJson<LearningPayload>(`/api/learning/applications?limit=${encodeURIComponent(String(limit))}`);
}

export async function getLearningLifecycle(limit = 30): Promise<LearningPayload> {
  return getJson<LearningPayload>(`/api/learning/lifecycle?limit=${encodeURIComponent(String(limit))}`);
}

export async function getEvolutionRuns(limit = 10): Promise<LearningPayload> {
  return getJson<LearningPayload>(`/api/learning/evolution/runs?limit=${encodeURIComponent(String(limit))}`);
}

export async function getParameterTemplatesActive(): Promise<LearningPayload> {
  return getJson<LearningPayload>("/api/learning/parameter-templates/active");
}

export async function getParameterTemplateSwitchLogs(limit = 20): Promise<LearningPayload> {
  return getJson<LearningPayload>(`/api/learning/parameter-templates/switch-logs?limit=${encodeURIComponent(String(limit))}`);
}

export async function getLearningReviews(limit = 10): Promise<LearningPayload> {
  return getJson<LearningPayload>(`/api/learning/reviews?limit=${encodeURIComponent(String(limit))}`);
}

export async function getAutonomousLearningSamples(limit = 10): Promise<LearningPayload> {
  return getJson<LearningPayload>(`/api/learning/autonomous/samples?limit=${encodeURIComponent(String(limit))}`);
}

export async function getMetaLightgbmShadowReport(limit = 80): Promise<LearningPayload> {
  return getJson<LearningPayload>(
    `/api/learning/model/meta-lightgbm/shadow-report?limit=${encodeURIComponent(String(limit))}&include_samples=false`,
  );
}

export async function getModelPermissionAudits(limit = 10): Promise<LearningPayload> {
  return getJson<LearningPayload>(`/api/learning/model/permissions/audits?limit=${encodeURIComponent(String(limit))}`);
}

export async function getLearningDatasetReadiness(): Promise<LearningPayload> {
  return getJson<LearningPayload>("/api/learning/dataset/readiness");
}

export async function getLearningDatasetQualityHealth(limit = 1000): Promise<LearningPayload> {
  return getJson<LearningPayload>(`/api/learning/dataset/quality-health?limit=${encodeURIComponent(String(limit))}`);
}

export async function getModelShadowQueue(limit = 30): Promise<LearningPayload> {
  return getJson<LearningPayload>(`/api/learning/model/shadow-queue?limit=${encodeURIComponent(String(limit))}`);
}

export async function getModelCanaryReviews(limit = 30): Promise<LearningPayload> {
  return getJson<LearningPayload>(`/api/learning/model/canary-review?limit=${encodeURIComponent(String(limit))}`);
}

export async function getModelInferenceAudits(limit = 30): Promise<LearningPayload> {
  return getJson<LearningPayload>(`/api/learning/model/inference?limit=${encodeURIComponent(String(limit))}`);
}

export async function getMetaModelAdvisories(limit = 30): Promise<LearningPayload> {
  return getJson<LearningPayload>(`/api/learning/model/meta/advisories?limit=${encodeURIComponent(String(limit))}`);
}

export async function getMetaLightgbmAudits(limit = 30): Promise<LearningPayload> {
  return getJson<LearningPayload>(`/api/learning/model/meta-lightgbm/audits?limit=${encodeURIComponent(String(limit))}`);
}

export async function getPositionQualityLightgbmAudits(limit = 30): Promise<LearningPayload> {
  return getJson<LearningPayload>(`/api/learning/model/position-quality-lightgbm/audits?limit=${encodeURIComponent(String(limit))}`);
}

export async function getOpenQualityLightgbmAudits(limit = 30): Promise<LearningPayload> {
  return getJson<LearningPayload>(`/api/learning/model/open-quality-lightgbm/audits?limit=${encodeURIComponent(String(limit))}`);
}

export async function getFactorGovernanceLightgbmAudits(limit = 30): Promise<LearningPayload> {
  return getJson<LearningPayload>(`/api/learning/model/factor-governance-lightgbm/audits?limit=${encodeURIComponent(String(limit))}`);
}

export async function getFactorGovernanceLightgbmAdvisories(limit = 30): Promise<LearningPayload> {
  return getJson<LearningPayload>(`/api/learning/model/factor-governance-lightgbm/advisories?limit=${encodeURIComponent(String(limit))}`);
}

export async function getOffmarketHighLoadAudits(limit = 30): Promise<LearningPayload> {
  return getJson<LearningPayload>(`/api/learning/model/offmarket-high-load/audits?limit=${encodeURIComponent(String(limit))}`);
}

export async function getStateSnapshot(): Promise<StateSnapshot> {
  return getJson<StateSnapshot>("/api/state");
}

export async function getReplayLatest(): Promise<Record<string, unknown>> {
  return getJson<Record<string, unknown>>("/api/ops/replay/latest");
}

export async function getReplayBarDecisions(limit = 30): Promise<Record<string, unknown>> {
  const params = new URLSearchParams({
    lookback_days: "7",
    limit: String(limit),
    offset: "0",
  });
  return getJson<Record<string, unknown>>(`/api/ops/replay/bar-decisions?${params.toString()}`);
}

export async function runReplayBarEvidence(decisionId = ""): Promise<Record<string, unknown>> {
  const params = new URLSearchParams({
    lookback_days: "1",
    limit: "1",
    warmup_bars: "40",
    post_bars: "24",
  });
  if (decisionId) {
    params.set("decision_id", decisionId);
    params.set("lookback_days", "7");
  }
  return postJson<Record<string, unknown>>(`/api/ops/replay/bar-preview?${params.toString()}`);
}

export async function getIncidentControl(): Promise<Record<string, unknown>> {
  return getJson<Record<string, unknown>>("/api/ops/incident-control");
}

export async function setIncidentControl(mode: string, reason: string): Promise<Record<string, unknown>> {
  return postJson<Record<string, unknown>>("/api/ops/incident-control", {
    mode,
    reason,
    confirm_thaw: mode === "normal",
  });
}

export async function getIncidentPlaybookLatest(): Promise<Record<string, unknown>> {
  return getJson<Record<string, unknown>>("/api/ops/incident-playbook/latest");
}

export async function runIncidentPlaybook(scenario = "governance_failure", severity = "medium"): Promise<Record<string, unknown>> {
  return postJson<Record<string, unknown>>("/api/ops/incident-playbook/run", {
    scenario,
    severity,
    created_by: "web:v15_cockpit",
  });
}

export async function getAutonomyScopeApprovalLatest(): Promise<Record<string, unknown>> {
  return getJson<Record<string, unknown>>("/api/ops/autonomy-health/scope-approvals/latest");
}

export async function getAutonomyScopeEnforcementLatest(): Promise<Record<string, unknown>> {
  return getJson<Record<string, unknown>>("/api/ops/autonomy-health/scope-enforcements/latest");
}

export async function enforceAutonomyScope(): Promise<Record<string, unknown>> {
  return postJson<Record<string, unknown>>("/api/ops/autonomy-health/scope-enforcements", {
    actor: "web:v15_cockpit",
    reason: "web_v15_cockpit_tightening_review",
  });
}

export async function getV15Phase0(): Promise<Record<string, unknown>> {
  return getJson<Record<string, unknown>>("/api/ops/v15/phase0");
}

export async function getReleaseLatest(): Promise<Record<string, unknown>> {
  return getJson<Record<string, unknown>>("/api/ops/release/latest");
}

export async function startReleaseRun(): Promise<Record<string, unknown>> {
  return postJson<Record<string, unknown>>("/api/ops/release/start", {
    release_class: "daily_autonomous_mutation",
    summary: { source: "web_v15_cockpit" },
    tests: [],
    rollback_ref: {},
    created_by: "web:v15_cockpit",
  });
}

export async function getReleaseApprovals(runId: string): Promise<Record<string, unknown>> {
  return getJson<Record<string, unknown>>(`/api/ops/release/${encodeURIComponent(runId)}/approvals`);
}

export async function getBrainState(refresh = false): Promise<Record<string, unknown>> {
  const query = refresh ? "?refresh=true" : "";
  return getJson<Record<string, unknown>>(`/api/ops/brain/state${query}`);
}

export async function getBrainMemory(refresh = false, limit = 50): Promise<Record<string, unknown>> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (refresh) params.set("refresh", "true");
  return getJson<Record<string, unknown>>(`/api/ops/brain/memory?${params.toString()}`);
}

export async function getBrainActionPlans(refresh = false, limit = 50): Promise<Record<string, unknown>> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (refresh) params.set("refresh", "true");
  return getJson<Record<string, unknown>>(`/api/ops/brain/action-plans?${params.toString()}`);
}

export async function getBrainActionPlanEvals(refresh = false, limit = 50): Promise<Record<string, unknown>> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (refresh) params.set("refresh", "true");
  return getJson<Record<string, unknown>>(`/api/ops/brain/action-plan-evals?${params.toString()}`);
}

export async function getBrainLowImpactExecutions(limit = 50): Promise<Record<string, unknown>> {
  const params = new URLSearchParams({ limit: String(limit) });
  return getJson<Record<string, unknown>>(`/api/ops/brain/low-impact-executions?${params.toString()}`);
}

export async function runBrainLowImpactExecution(): Promise<Record<string, unknown>> {
  return postJson<Record<string, unknown>>("/api/ops/brain/low-impact-executions/run", {
    limit: 1,
    allow_tighten: false,
    replay_lookback_days: 1,
    replay_limit: 100,
  });
}

export async function getBrainMediumImpactGovernance(limit = 50): Promise<Record<string, unknown>> {
  const params = new URLSearchParams({ limit: String(limit) });
  return getJson<Record<string, unknown>>(`/api/ops/brain/medium-impact-governance?${params.toString()}`);
}

export async function getBrainGovernanceCandidateReviews(limit = 50): Promise<Record<string, unknown>> {
  const params = new URLSearchParams({ limit: String(limit) });
  return getJson<Record<string, unknown>>(`/api/ops/brain/governance-candidate-reviews?${params.toString()}`);
}

export async function materializeBrainMediumImpactGovernance(): Promise<Record<string, unknown>> {
  return postJson<Record<string, unknown>>("/api/ops/brain/medium-impact-governance/materialize", {
    limit: 4,
    allow_tighten_low_health: false,
  });
}

export async function reviewBrainGovernanceCandidates(): Promise<Record<string, unknown>> {
  return postJson<Record<string, unknown>>("/api/ops/brain/governance-candidates/review", {
    limit: 20,
    run_llm: false,
    llm_dry_run: true,
  });
}

export async function getBrainLiveReadyGuardrails(limit = 50): Promise<Record<string, unknown>> {
  const params = new URLSearchParams({ limit: String(limit) });
  return getJson<Record<string, unknown>>(`/api/ops/brain/live-ready-guardrails?${params.toString()}`);
}

export async function getAutonomyProposals(
  refresh = false,
  limit = 80,
  status = "",
): Promise<Record<string, unknown>> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (refresh) params.set("refresh", "true");
  if (status) params.set("status", status);
  return getJson<Record<string, unknown>>(`/api/ops/autonomy/proposals?${params.toString()}`);
}

export async function refreshAutonomyProposals(limit = 500): Promise<Record<string, unknown>> {
  const params = new URLSearchParams({ limit: String(limit) });
  return postJson<Record<string, unknown>>(`/api/ops/autonomy/proposals/refresh?${params.toString()}`, {});
}

export async function reviewAutonomyProposal(
  proposalId: string,
  payload: { decision?: string; route?: string; notes?: string; actor?: string } = {},
): Promise<Record<string, unknown>> {
  return postJson<Record<string, unknown>>(`/api/ops/autonomy/proposals/${encodeURIComponent(proposalId)}/review`, {
    actor: payload.actor || "web:meta_governance",
    decision: payload.decision || "reviewed",
    route: payload.route || "",
    notes: payload.notes || "",
  });
}

export async function getLiveAutonomyStatus(refreshProposals = false): Promise<Record<string, unknown>> {
  const params = new URLSearchParams();
  if (refreshProposals) params.set("refresh_proposals", "true");
  const suffix = params.toString() ? `?${params.toString()}` : "";
  return getJson<Record<string, unknown>>(`/api/ops/autonomy/live-status${suffix}`);
}

export async function evaluateLiveAutonomyUnlock(
  reason = "web_meta_governance_evaluate",
): Promise<Record<string, unknown>> {
  return postJson<Record<string, unknown>>("/api/ops/autonomy/live-unlock/evaluate", {
    actor: "web:meta_governance",
    reason,
    confirm: false,
  });
}

export async function unlockLiveAutonomy(reason = "web_meta_governance_unlock"): Promise<Record<string, unknown>> {
  return postJson<Record<string, unknown>>("/api/ops/autonomy/live-unlock", {
    actor: "web:meta_governance",
    reason,
    confirm: true,
  });
}

export async function revokeLiveAutonomy(reason = "web_meta_governance_revoke"): Promise<Record<string, unknown>> {
  return postJson<Record<string, unknown>>("/api/ops/autonomy/live-unlock/revoke", {
    actor: "web:meta_governance",
    reason,
  });
}

export async function evaluateBrainLiveReadyGuardrail(): Promise<Record<string, unknown>> {
  return postJson<Record<string, unknown>>("/api/ops/brain/live-ready-guardrails/evaluate", {
    source: "web:v16_brain",
  });
}

export async function tightenBrainLiveReadyGuardrail(targetMode: string): Promise<Record<string, unknown>> {
  return postJson<Record<string, unknown>>("/api/ops/brain/live-ready-guardrails/tighten", {
    target_mode: targetMode,
    reason: `web:v16_brain:${targetMode}`,
    actor: "web:v16_brain",
  });
}

export async function startTrading(
  broker = "ctrader",
  strategy_name = "factor_v4",
  confirmed = false,
): Promise<Record<string, unknown>> {
  return postJson(
    "/api/live/start",
    { broker, strategy_name },
    confirmed ? { "X-Confirm": "start-live" } : undefined,
  );
}

export async function stopTrading(): Promise<Record<string, unknown>> {
  return postJson("/api/live/stop", {});
}

export async function emergencyClose(confirmed = false): Promise<Record<string, unknown>> {
  return apiRequest<Record<string, unknown>>("/api/live/emergency-close", {
    method: "POST",
    headers: {
      ...(confirmed ? { "X-Confirm": "emergency" } : {}),
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ broker: "ctrader", symbol: null }),
  });
}

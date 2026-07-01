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

export async function getBackendReadiness(): Promise<BackendReadinessPayload> {
  return getJson<BackendReadinessPayload>("/api/ops/backend-readiness");
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

export async function getStateSnapshot(): Promise<StateSnapshot> {
  return getJson<StateSnapshot>("/api/state");
}

export async function startTrading(broker = "ctrader", strategy_name = "factor_v4"): Promise<Record<string, unknown>> {
  return postJson("/api/live/start", { broker, strategy_name });
}

export async function stopTrading(): Promise<Record<string, unknown>> {
  return postJson("/api/live/stop", {});
}

export async function emergencyClose(): Promise<Record<string, unknown>> {
  return apiRequest<Record<string, unknown>>("/api/live/emergency-close", {
    method: "POST",
    headers: {
      "X-Confirm": "emergency",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ broker: "ctrader", symbol: null }),
  });
}

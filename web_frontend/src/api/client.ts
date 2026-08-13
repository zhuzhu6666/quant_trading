import { clearAccessToken, getAccessToken, setAccessToken } from "@/auth/tokenStore";
import { deleteRefreshMaterial, readRefreshMaterial, storeRefreshMaterial } from "@/desktop/bridge";
import { syncServerClockFromPayload } from "@/api/time";
import { isTauri } from "@tauri-apps/api/core";

export type AuthError = {
  message: string;
  status: number;
  detail?: unknown;
};

type UnknownObject = { [key: string]: unknown };

function object(value: unknown): UnknownObject {
  return value && typeof value === "object" && !Array.isArray(value) ? value as UnknownObject : {};
}

export function getApiErrorCode(error: unknown): string {
  const detail = object(object(error).detail);
  const direct = detail.error;
  if (typeof direct === "string") return direct;
  const nested = object(detail.detail).error;
  return typeof nested === "string" ? nested : "";
}

export function isStepUpRequiredError(error: unknown): boolean {
  return getApiErrorCode(error) === "step_up_required";
}

export type LoginPayload = { username: string; password: string };
export type LoginResponse = { user: string; access_token?: string; token?: string; refresh_token?: string; expires_in?: number; token_type?: string };
export type StepUpResponse = LoginResponse & { session_id: string; auth_time: number };
export type AuthMe = { user: string; authenticated: boolean };

export function extractLoginToken(response: LoginResponse): string {
  const value = response.access_token ?? response.token ?? "";
  return value.startsWith("Bearer ") ? value.slice(7) : value;
}

const DEFAULT_DESKTOP_API_BASE_URL = "https://www.zhuzhu666.icu";
const envBase = typeof import.meta !== "undefined" && import.meta.env ? String(import.meta.env.VITE_API_BASE_URL || "") : "";
// Browser production is same-origin behind Caddy; Tauri must target the remote API explicitly.
// VITE_API_BASE_URL remains the deployment override for staging or local backends.
const configuredBase = envBase || (isTauri() ? DEFAULT_DESKTOP_API_BASE_URL : "");
const API_BASE = configuredBase.replace(/\/$/, "");
const AUTH_REFRESH_LOCK = "quant.auth.refresh";
let unauthorizedHandler: (() => void | Promise<void>) | null = null;
let unauthorizedInFlight: Promise<void> | null = null;
let refreshInFlight: Promise<string | null> | null = null;

export function setUnauthorizedHandler(handler: () => void | Promise<void>): void {
  unauthorizedHandler = handler;
}

export function resetUnauthorizedCoordinator(): void {
  unauthorizedInFlight = null;
}

async function runUnauthorizedOnce(): Promise<void> {
  if (!unauthorizedInFlight) {
    unauthorizedInFlight = (async () => {
      if (unauthorizedHandler) await unauthorizedHandler();
      else clearAccessToken();
    })();
  }
  await unauthorizedInFlight;
}

export function getApiBaseUrl(): string {
  return API_BASE;
}

function httpUrl(path: string): string {
  const normalized = path.startsWith("/") ? path : `/${path}`;
  return `${API_BASE}${normalized}`;
}

function wsUrl(path: string): string {
  const normalized = path.startsWith("/") ? path : `/${path}`;
  if (API_BASE && /^https?:\/\//i.test(API_BASE)) {
    const parsed = new URL(API_BASE);
    return `${parsed.protocol === "https:" ? "wss:" : "ws:"}//${parsed.host}${normalized}`;
  }
  const protocol = typeof window !== "undefined" && window.location.protocol === "https:" ? "wss:" : "ws:";
  const host = typeof window !== "undefined" ? window.location.host : "localhost:5173";
  return `${protocol}//${host}${normalized}`;
}

export function getWsUrl(): string {
  return wsUrl("/ws/state");
}

async function parseResponse<T>(response: Response): Promise<T> {
  if (response.status === 204) return null as T;
  const contentType = response.headers.get("content-type") ?? "";
  return contentType.includes("application/json") ? response.json() as Promise<T> : response.text() as Promise<T>;
}

async function refreshFromServer(failedToken: string | null): Promise<string | null> {
  const refresh = async () => {
    const current = getAccessToken();
    if (current && current !== failedToken) return current;
    const refreshMaterial = await readRefreshMaterial();
    const response = await fetch(httpUrl("/api/auth/refresh"), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(refreshMaterial ? { refresh_token: refreshMaterial } : {}), credentials: "include" });
    if (!response.ok) return null;
    const result = await parseResponse<LoginResponse>(response);
    const token = extractLoginToken(result);
    if (!token) return null;
    if (result.refresh_token) void storeRefreshMaterial(result.refresh_token);
    setAccessToken(token);
    window.dispatchEvent(new CustomEvent("quant-auth-token", { detail: { token } }));
    resetUnauthorizedCoordinator();
    return token;
  };
  if (typeof navigator !== "undefined" && navigator.locks) return navigator.locks.request(AUTH_REFRESH_LOCK, refresh);
  return refresh();
}

export async function refreshSession(): Promise<string | null> {
  if (!refreshInFlight) refreshInFlight = refreshFromServer(getAccessToken()).catch(() => null).finally(() => { refreshInFlight = null; });
  return refreshInFlight;
}

export async function apiRequest<T>(path: string, init: RequestInit = {}, retried = false): Promise<T> {
  const token = getAccessToken();
  const headers = new Headers(init.headers ?? {});
  if (init.body !== undefined && init.body !== null && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (token && !headers.has("Authorization")) headers.set("Authorization", `Bearer ${token}`);
  const requestToken = headers.get("Authorization")?.startsWith("Bearer ") ? headers.get("Authorization")!.slice(7) : token;
  const response = await fetch(httpUrl(path), { ...init, headers, credentials: "include" });
  if (response.status === 401) {
    const detail = await parseResponse<unknown>(response);
    const authEndpoint = path.startsWith("/api/auth/login") || path.startsWith("/api/auth/refresh");
    if (!retried && !authEndpoint) {
      const current = getAccessToken();
      const refreshed = current && current !== requestToken ? current : await refreshSession();
      if (refreshed) {
        const retryHeaders = new Headers(init.headers ?? {});
        retryHeaders.delete("Authorization");
        return apiRequest<T>(path, { ...init, headers: retryHeaders }, true);
      }
    }
    await runUnauthorizedOnce();
    throw Object.assign(new Error("unauthorized"), { status: 401, detail, message: "Unauthorized" } satisfies AuthError);
  }
  if (!response.ok) {
    const detail = await parseResponse<unknown>(response);
    throw Object.assign(new Error("request failed"), { status: response.status, detail, message: response.statusText } satisfies AuthError);
  }
  const result = await parseResponse<T>(response);
  syncServerClockFromPayload(result);
  return result;
}

export const getJson = <T>(path: string): Promise<T> => apiRequest<T>(path, { method: "GET" });
export const postJson = <T>(path: string, body?: unknown, extraHeaders?: HeadersInit): Promise<T> => apiRequest<T>(path, { method: "POST", headers: extraHeaders, body: body === undefined ? undefined : JSON.stringify(body) });

export async function login(payload: LoginPayload): Promise<LoginResponse> {
  const result = await postJson<LoginResponse>("/api/auth/login", payload);
  const token = extractLoginToken(result);
  if (!token) throw new Error("登录响应缺少 token");
  if (result.refresh_token) void storeRefreshMaterial(result.refresh_token);
  setAccessToken(token);
  window.dispatchEvent(new CustomEvent("quant-auth-token", { detail: { token } }));
  return result;
}

export async function stepUpAuth(password: string): Promise<StepUpResponse> {
  const result = await postJson<StepUpResponse>("/api/auth/step-up", { password });
  const token = extractLoginToken(result);
  if (!token) throw new Error("step-up 响应缺少 access token");
  setAccessToken(token);
  window.dispatchEvent(new CustomEvent("quant-auth-token", { detail: { token } }));
  resetUnauthorizedCoordinator();
  return result;
}

export const getAuthMe = () => getJson<AuthMe>("/api/auth/me");
export async function logoutAuth(): Promise<void> {
  try { await postJson<unknown>("/api/auth/logout", {}); } finally { await deleteRefreshMaterial(); }
}
export const getWsTicket = () => postJson<{ ticket: string; expires_in: number; expires_at: number }>("/api/auth/ws-ticket", {});

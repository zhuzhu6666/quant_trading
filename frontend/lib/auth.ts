"use client";

/**
 * JWT auth wiring. (audit 2026-06-08: previously the login page stored the
 * token in localStorage but no code ever read it. Combined with the backend
 * lenient `get_current_user` (returns "zhu" if no token), this meant the
 * token was silently dead. After the v8 fix every API endpoint requires a
 * valid Bearer token via `require_user`, so the frontend must attach it to
 * every fetch.)
 *
 * v1: any password is accepted by /api/auth/login. The returned token is a
 * real HS256 JWT (24h expiry) and is required for /api/*.
 */

const TOKEN_KEY = "quant_token";
const USER_KEY = "quant_user";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(TOKEN_KEY);
}

export function getUser(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(USER_KEY);
}

export function setAuth(token: string, user: string): void {
  if (typeof window === "undefined") return;
  localStorage.setItem(TOKEN_KEY, token);
  localStorage.setItem(USER_KEY, user);
}

export function clearAuth(): void {
  if (typeof window === "undefined") return;
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
}

/** True iff a token is present in localStorage. Does NOT verify the token
 * is still valid (server returns 401 if expired). */
export function hasToken(): boolean {
  return !!getToken();
}

/**
 * Drop-in fetch replacement that automatically attaches the Bearer token.
 * On 401 we clear the stale token and reload the page so the user lands on
 * /login (the root layout renders Sidebar/Topbar which will then mount with
 * no auth state).
 *
 * Throws Error on non-2xx (after reading the response body once) so callers
 * can do `try { await authFetch(...) } catch (e) { alert(e.message) }` —
 * matches the pattern used in every existing page handler.
 */
export async function authFetch(
  input: string,
  init: RequestInit = {},
): Promise<Response> {
  const token = getToken();
  const headers = new Headers(init.headers || {});
  if (token) headers.set("Authorization", "Bearer " + token);
  if (!headers.has("Content-Type") && init.body && typeof init.body === "string") {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(input, { ...init, headers });
  if (res.status === 401) {
    // Token missing, expired, or invalid. Clear it and force re-login.
    clearAuth();
    if (typeof window !== "undefined" && !window.location.pathname.startsWith("/login")) {
      window.location.assign("/login");
    }
  }
  return res;
}

/** Read JSON body of a 401-aware fetch. Throws on non-2xx with backend's
 * `detail.msg` if present. */
export async function authJson<T = any>(
  input: string,
  init: RequestInit = {},
): Promise<T> {
  const res = await authFetch(input, init);
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const e = await res.json();
      msg = e?.detail?.msg || e?.detail || e?.error || msg;
    } catch {}
    throw new Error(msg);
  }
  return res.json();
}

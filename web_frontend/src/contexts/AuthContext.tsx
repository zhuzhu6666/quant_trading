import { createContext, ReactNode, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  AuthMe,
  getAuthMe,
  login as apiLogin,
  LoginPayload,
  extractLoginToken,
  logoutAuth,
  refreshSession,
  resetUnauthorizedCoordinator,
  setUnauthorizedHandler,
} from "@/api/client";
import { queryClient } from "@/api/queryClient";
import { clearAccessToken as clearMemoryAccessToken, getAccessToken, setAccessToken } from "@/auth/tokenStore";
import { authStateAfterMeFailure, type AuthSnapshot } from "@/contexts/authState";
import { deleteRefreshMaterial } from "@/desktop/bridge";

type AuthContextValue = AuthSnapshot & {
  login: (payload: LoginPayload) => Promise<void>;
  logout: () => void;
  refreshMe: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

async function clearSessionState(clearToken: () => void): Promise<void> {
  await queryClient.cancelQueries();
  queryClient.clear();
  await deleteRefreshMaterial();
  clearToken();
  window.dispatchEvent(new Event("quant-auth-invalidated"));
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const [state, setState] = useState<AuthSnapshot>({
    token: getAccessToken(),
    user: null,
    loading: true,
    authenticated: false,
  });

  const clearLocalSession = useCallback(() => {
    clearMemoryAccessToken();
    setState((previous) => ({ ...previous, token: null, user: null, authenticated: false }));
  }, []);

  const logout = useCallback(() => {
    void logoutAuth().catch(() => undefined);
    void clearSessionState(clearLocalSession);
    navigate("/login", { replace: true });
  }, [clearLocalSession, navigate]);

  const refreshMe = useCallback(async () => {
    let token = getAccessToken();
    if (!token) {
      token = await refreshSession();
    }
    if (!token) {
      setState((previous) => ({ ...previous, token: null, loading: false, authenticated: false, user: null }));
      return;
    }
    setState((previous) => ({ ...previous, token }));
    try {
      const me: AuthMe = await getAuthMe();
      setState((previous) => ({ ...previous, token, user: me.user, authenticated: me.authenticated, loading: false }));
    } catch (error) {
      const status = (error as { status?: number } | null)?.status;
      setState((previous) => authStateAfterMeFailure(previous, status));
    }
  }, []);

  useEffect(() => {
    void refreshMe();
  }, [refreshMe]);

  const login = useCallback(async (payload: LoginPayload) => {
    const result = await apiLogin(payload);
    const token = extractLoginToken(result);
    if (!token) throw new Error("登录响应缺少 token");
    setAccessToken(token);
    resetUnauthorizedCoordinator();
    setState((previous) => ({
      ...previous,
      token,
      loading: false,
      authenticated: true,
      user: result.user,
    }));
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(async () => {
      await clearSessionState(clearLocalSession);
      navigate("/login", { replace: true });
    });
  }, [clearLocalSession, navigate]);

  useEffect(() => {
    const channel = typeof BroadcastChannel === "undefined" ? null : new BroadcastChannel("quant-auth-events");
    const onInvalidated = () => {
      clearLocalSession();
      navigate("/login", { replace: true });
    };
    window.addEventListener("quant-auth-invalidated", onInvalidated);
    channel?.addEventListener("message", (event) => {
      if (event.data?.type === "logout") onInvalidated();
    });
    return () => {
      window.removeEventListener("quant-auth-invalidated", onInvalidated);
      channel?.close();
    };
  }, [clearLocalSession, navigate]);

  const value = useMemo<AuthContextValue>(() => ({ ...state, login, logout, refreshMe }), [login, logout, refreshMe, state]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used within AuthProvider");
  return context;
}

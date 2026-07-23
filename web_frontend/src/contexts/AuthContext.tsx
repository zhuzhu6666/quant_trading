import { createContext, ReactNode, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  AuthMe,
  getAuthMe,
  login as apiLogin,
  LoginPayload,
  extractLoginToken,
  logoutAuth,
  resetUnauthorizedCoordinator,
  setUnauthorizedHandler,
} from "@/api/client";
import { queryClient } from "@/api/queryClient";
import { authStateAfterMeFailure, type AuthSnapshot } from "@/contexts/authState";

type AuthState = AuthSnapshot;

type AuthContextValue = AuthState & {
  login: (payload: LoginPayload) => Promise<void>;
  logout: () => void;
  refreshMe: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

const STORAGE_KEY = "quant.auth.token";

export function AuthProvider({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const [state, setState] = useState<AuthState>({
    token: localStorage.getItem(STORAGE_KEY),
    user: null,
    loading: true,
    authenticated: false,
  });

  const clearLocalSession = useCallback(() => {
    localStorage.removeItem(STORAGE_KEY);
    setState((prev) => ({ ...prev, token: null, user: null, authenticated: false }));
    window.dispatchEvent(new Event("quant-auth-invalidated"));
  }, []);

  const logout = () => {
    void logoutAuth().catch(() => undefined);
    void queryClient.cancelQueries();
    queryClient.clear();
    clearLocalSession();
    navigate("/login", { replace: true });
  };

  const refreshMe = async () => {
    if (!state.token) {
      setState((prev) => ({ ...prev, loading: false, authenticated: false, user: null }));
      return;
    }
    try {
      const me: AuthMe = await getAuthMe();
      setState((prev) => ({ ...prev, user: me.user, authenticated: me.authenticated, loading: false }));
    } catch (error) {
      const status = (error as { status?: number } | null)?.status;
      setState((prev) => authStateAfterMeFailure(prev, status));
    }
  };

  useEffect(() => {
    refreshMe();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.token]);

  const login = async (payload: LoginPayload) => {
    const result = await apiLogin(payload);
    const token = extractLoginToken(result);
    if (!token) {
      throw new Error("登录响应缺少 token");
    }
    localStorage.setItem(STORAGE_KEY, token);
    resetUnauthorizedCoordinator();
    setState((prev) => ({
      ...prev,
      token,
      loading: false,
      authenticated: true,
      user: result.user,
    }));
  };

  useEffect(() => {
    setUnauthorizedHandler(async () => {
      await queryClient.cancelQueries();
      queryClient.clear();
      clearLocalSession();
      navigate("/login", { replace: true });
    });
  }, [clearLocalSession, navigate]);

  useEffect(() => {
    const onToken = (event: Event) => {
      const token = (event as CustomEvent<{ token?: string }>).detail?.token || "";
      if (token) {
        setState((prev) => ({ ...prev, token }));
      }
    };
    const onStorage = (event: StorageEvent) => {
      if (event.key !== STORAGE_KEY) return;
      const token = event.newValue || null;
      setState((prev) => ({
        ...prev,
        token,
        user: token ? prev.user : null,
        authenticated: token ? prev.authenticated : false,
      }));
    };
    window.addEventListener("quant-auth-token", onToken);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener("quant-auth-token", onToken);
      window.removeEventListener("storage", onStorage);
    };
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      ...state,
      login,
      logout,
      refreshMe,
    }),
    [state],
  );

  return (
    <AuthContext.Provider value={value}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used within AuthProvider");
  }
  return ctx;
}

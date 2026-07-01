import { createContext, ReactNode, useContext, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  AuthMe,
  getAuthMe,
  login as apiLogin,
  LoginPayload,
  extractLoginToken,
  setUnauthorizedHandler,
} from "@/api/client";

type AuthState = {
  token: string | null;
  user: string | null;
  loading: boolean;
  authenticated: boolean;
};

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

  const logout = () => {
    localStorage.removeItem(STORAGE_KEY);
    setState((prev) => ({ ...prev, token: null, user: null, authenticated: false }));
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
    } catch {
      logout();
      setState((prev) => ({ ...prev, loading: false }));
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
    setState((prev) => ({
      ...prev,
      token,
      loading: false,
      authenticated: true,
      user: result.user,
    }));
  };

  useEffect(() => {
    setUnauthorizedHandler(() => () => {
      logout();
      navigate("/login", { replace: true });
    });
  }, [navigate]);

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

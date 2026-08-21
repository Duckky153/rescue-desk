"use client";

import { useRouter } from "next/navigation";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { ApiError, apiRequest, clearToken, getStoredToken, storeToken } from "@/lib/api";
import type { CurrentUser, TokenResponse } from "@/types/api";

interface AuthContextValue {
  user: CurrentUser | null;
  loading: boolean;
  login: (email: string, password: string, organizationId?: string) => Promise<void>;
  logout: () => void;
  refreshUser: () => Promise<void>;
  restoreError: string | null;
  retryRestore: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [loading, setLoading] = useState(true);
  const [restoreError, setRestoreError] = useState<string | null>(null);
  const router = useRouter();

  const refreshUser = useCallback(async () => {
    const current = await apiRequest<CurrentUser>("/v1/auth/me");
    setUser(current);
  }, []);

  const retryRestore = useCallback(async () => {
    if (!getStoredToken()) {
      setRestoreError(null);
      setLoading(false);
      return;
    }
    setLoading(true);
    setRestoreError(null);
    try {
      const current = await apiRequest<CurrentUser>("/v1/auth/me");
      setUser(current);
    } catch (error) {
      if (error instanceof ApiError && [401, 403].includes(error.status)) {
        clearToken();
        setUser(null);
      } else {
        setRestoreError(
          `Your saved session could not be verified (${error instanceof Error ? error.message : "temporary connection failure"}). Retry without signing in again.`,
        );
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let active = true;
    async function restore() {
      if (!getStoredToken()) {
        if (active) setLoading(false);
        return;
      }
      try {
        const current = await apiRequest<CurrentUser>("/v1/auth/me");
        if (active) setUser(current);
      } catch (error) {
        if (!active) return;
        if (error instanceof ApiError && [401, 403].includes(error.status)) {
          clearToken();
          setUser(null);
        } else {
          setRestoreError(
            `Your saved session could not be verified (${error instanceof Error ? error.message : "temporary connection failure"}). Retry without signing in again.`,
          );
        }
      } finally {
        if (active) setLoading(false);
      }
    }
    void restore();
    return () => {
      active = false;
    };
  }, []);

  const login = useCallback(
    async (email: string, password: string, organizationId?: string) => {
      const token = await apiRequest<TokenResponse>(
        "/v1/auth/token",
        {
          method: "POST",
          body: JSON.stringify({ email, password, organization_id: organizationId || null }),
        },
        { auth: false },
      );
      storeToken(token.access_token);
      await refreshUser();
      router.replace("/");
    },
    [refreshUser, router],
  );

  const logout = useCallback(() => {
    clearToken();
    setUser(null);
    router.replace("/login");
  }, [router]);

  const value = useMemo(
    () => ({ user, loading, login, logout, refreshUser, restoreError, retryRestore }),
    [user, loading, login, logout, refreshUser, restoreError, retryRestore],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside AuthProvider");
  return context;
}

export function RequireAuth({ children }: { children: ReactNode }) {
  const { user, loading, restoreError, retryRestore } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!loading && !user && !restoreError) router.replace("/login");
  }, [loading, restoreError, router, user]);

  if (!loading && !user && restoreError) {
    return (
      <main className="loading-screen" aria-live="polite">
        <div className="alert danger" role="alert">
          <strong>Session check interrupted.</strong> {restoreError}
        </div>
        <button className="button" type="button" onClick={() => void retryRestore()}>
          Retry session check
        </button>
      </main>
    );
  }

  if (loading || !user) {
    return (
      <div className="loading-screen" aria-live="polite">
        <div className="spinner" aria-label="Loading" />
      </div>
    );
  }
  return children;
}

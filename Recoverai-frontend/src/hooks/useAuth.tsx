import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { apiFetch, ApiError } from "@/lib/api";

/**
 * Real auth, backed by Django's session-cookie login (session/login/logout/
 * me under /api/auth/*, see recovery_agent/views_auth.py). Single-admin
 * MVP: there's no signup call here on purpose — the admin user is created
 * with `python manage.py createsuperuser`.
 */

type AuthUser = {
  id: number;
  username: string;
  email: string;
  name: string;
  is_staff: boolean;
  is_superuser: boolean;
} | null;

type AuthContextValue = {
  user: AuthUser;
  isLoading: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser>(null);
  const [isLoading, setIsLoading] = useState(true);

  // On first load: prime the CSRF cookie, then check for an existing session
  // (so a page refresh doesn't bounce a signed-in admin back to /login).
  useEffect(() => {
    (async () => {
      try {
        await apiFetch("/auth/csrf/");
        const res = await apiFetch<{ user: AuthUser }>("/auth/me/");
        setUser(res.user);
      } catch {
        setUser(null);
      } finally {
        setIsLoading(false);
      }
    })();
  }, []);

  const login = async (username: string, password: string) => {
    try {
      const res = await apiFetch<{ user: AuthUser }>("/auth/login/", {
        method: "POST",
        body: JSON.stringify({ username, password }),
      });
      setUser(res.user);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        throw new Error("Invalid username or password");
      }
      throw new Error("Couldn't reach the server. Is the backend running?");
    }
  };

  const logout = async () => {
    try {
      await apiFetch("/auth/logout/", { method: "POST" });
    } finally {
      setUser(null);
    }
  };

  return (
    <AuthContext.Provider value={{ user, isLoading, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}

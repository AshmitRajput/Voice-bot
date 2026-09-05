const BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000";

async function handle<T>(res: Response, method: string, path: string): Promise<T> {
  if (!res.ok) {
    let detail = "";
    try {
      detail = await res.text();
    } catch {
      /* ignore */
    }
    throw new Error(`${method} ${path} failed: ${res.status}${detail ? ` — ${detail}` : ""}`);
  }
  return res.json();
}

export async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`);
  return handle<T>(res, "GET", path);
}

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return handle<T>(res, "POST", path);
}

export async function apiPatch<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return handle<T>(res, "PATCH", path);
}

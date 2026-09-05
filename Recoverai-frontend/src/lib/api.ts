/**
 * Thin fetch wrapper for the RecoverAI Django backend.
 *
 * - Always sends credentials (cookies) so the Django session persists.
 * - Reads the `csrftoken` cookie Django sets via ensure_csrf_cookie and
 *   echoes it back as X-CSRFToken on any unsafe method, which is what
 *   DRF's SessionAuthentication (already configured in settings.py)
 *   requires.
 * - Requests go through Vite's dev proxy (/api -> 127.0.0.1:8000), so in
 *   dev this is same-origin from the browser's point of view.
 */

function getCookie(name: string): string | null {
  const match = document.cookie.match(new RegExp("(^| )" + name + "=([^;]+)"));
  return match ? decodeURIComponent(match[2]) : null;
}

const UNSAFE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export class ApiError extends Error {
  status: number;
  body: unknown;
  constructor(message: string, status: number, body: unknown) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

export async function apiFetch<T = unknown>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);

  if (!headers.has("Content-Type") && init.body) {
    headers.set("Content-Type", "application/json");
  }
  if (UNSAFE_METHODS.has(method)) {
    const csrftoken = getCookie("csrftoken");
    if (csrftoken) headers.set("X-CSRFToken", csrftoken);
  }

  const res = await fetch(`/api${path}`, {
    ...init,
    method,
    headers,
    credentials: "include",
  });

  const contentType = res.headers.get("content-type") ?? "";
  const data = contentType.includes("application/json") ? await res.json() : null;

  if (!res.ok) {
    const message = (data && (data as any).error) || res.statusText;
    throw new ApiError(message, res.status, data);
  }
  return data as T;
}

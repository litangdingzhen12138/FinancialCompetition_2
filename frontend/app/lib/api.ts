import type {
  AuditItem,
  AnswerMode,
  AdminUserSummary,
  HistoryItem,
  Insight,
  QueryResponse,
  TimeGranularity,
} from "../types";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000";

const AUTH_TOKEN_KEY = "bankinsight.auth.token";
const AUTH_USER_KEY = "bankinsight.auth.user";
let cachedAuthUser: AuthUser | null = null;

export type AuthUser = {
  user_id: string;
  username: string;
  display_name: string;
  role: "analyst" | "admin";
};

function rememberAuthUser(user: AuthUser | null): void {
  cachedAuthUser = user;
  if (typeof window === "undefined") return;
  if (user) window.localStorage.setItem(AUTH_USER_KEY, JSON.stringify(user));
  else window.localStorage.removeItem(AUTH_USER_KEY);
}

export function getRememberedAuthUser(): AuthUser | null {
  return cachedAuthUser;
}

export function getCachedAuthUser(): AuthUser {
  if (cachedAuthUser) return cachedAuthUser;
  if (typeof window !== "undefined") {
    const stored = window.localStorage.getItem(AUTH_USER_KEY);
    if (stored) {
      cachedAuthUser = JSON.parse(stored) as AuthUser;
      return cachedAuthUser;
    }
  }
  throw new Error("当前页面必须在登录后访问");
}

function authHeaders(): Record<string, string> {
  if (typeof window === "undefined") return {};
  const token = window.localStorage.getItem(AUTH_TOKEN_KEY);
  return token ? { Authorization: `Bearer ${token}` } : {};
}

type StreamHandlers = {
  onStatus: (payload: {
    stage: string;
    message: string;
    session_id?: string;
  }) => void;
  onResult: (payload: QueryResponse) => void;
  onComplete: (payload: { query_id: string }) => void;
};

type FinalAnswerHandlers = {
  onStart: (payload: { query_id: string; mode: AnswerMode }) => void;
  onDelta: (payload: { query_id: string; content: string }) => void;
  onDone: (payload: {
    query_id: string;
    mode: AnswerMode;
    answer: string;
    insight: Insight;
  }) => void;
};

async function errorMessage(response: Response): Promise<string> {
  try {
    const payload = (await response.json()) as { detail?: string };
    return payload.detail || `请求失败（${response.status}）`;
  } catch {
    return `请求失败（${response.status}）`;
  }
}

export async function login(
  username: string,
  password: string,
): Promise<AuthUser> {
  const response = await fetch(`${API_BASE}/api/v1/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!response.ok) throw new Error(await errorMessage(response));
  const payload = (await response.json()) as {
    access_token: string;
    user: AuthUser;
  };
  window.localStorage.setItem(AUTH_TOKEN_KEY, payload.access_token);
  rememberAuthUser(payload.user);
  return payload.user;
}

export async function getCurrentUser(): Promise<AuthUser | null> {
  if (typeof window === "undefined" || !window.localStorage.getItem(AUTH_TOKEN_KEY)) {
    return null;
  }
  const response = await fetch(`${API_BASE}/api/v1/auth/me`, {
    headers: authHeaders(),
    cache: "no-store",
  });
  if (!response.ok) {
    window.localStorage.removeItem(AUTH_TOKEN_KEY);
    rememberAuthUser(null);
    return null;
  }
  const user = (await response.json()) as AuthUser;
  rememberAuthUser(user);
  return user;
}

export async function logout(): Promise<void> {
  try {
    const response = await fetch(`${API_BASE}/api/v1/auth/logout`, {
      method: "POST",
      headers: authHeaders(),
    });
    if (!response.ok && response.status !== 401) {
      throw new Error(await errorMessage(response));
    }
  } finally {
    window.localStorage.removeItem(AUTH_TOKEN_KEY);
    rememberAuthUser(null);
  }
}

export async function checkCapabilities(): Promise<boolean> {
  try {
    const response = await fetch(`${API_BASE}/api/v1/capabilities`, {
      cache: "no-store",
    });
    return response.ok;
  } catch {
    return false;
  }
}

export async function clearSession(
  sessionId: string,
  ownerUserId?: string,
): Promise<void> {
  const parameters = new URLSearchParams();
  if (ownerUserId) parameters.set("owner_user_id", ownerUserId);
  const suffix = parameters.size ? `?${parameters}` : "";
  const response = await fetch(
    `${API_BASE}/api/v1/sessions/${encodeURIComponent(sessionId)}${suffix}`,
    {
      method: "DELETE",
      headers: authHeaders(),
    },
  );
  if (!response.ok) throw new Error(await errorMessage(response));
}

export async function renameSession(
  sessionId: string,
  title: string,
  ownerUserId?: string,
): Promise<void> {
  const response = await fetch(
    `${API_BASE}/api/v1/sessions/${encodeURIComponent(sessionId)}`,
    {
      method: "PATCH",
      headers: {
        "Content-Type": "application/json",
        ...authHeaders(),
      },
      body: JSON.stringify({ title, owner_user_id: ownerUserId }),
    },
  );
  if (!response.ok) throw new Error(await errorMessage(response));
}

export async function streamQuery(
  question: string,
  sessionId: string | null,
  handlers: StreamHandlers,
): Promise<void> {
  const response = await fetch(`${API_BASE}/api/v1/queries/stream`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...authHeaders(),
    },
    body: JSON.stringify({ question, session_id: sessionId }),
  });
  if (!response.ok || !response.body) {
    throw new Error(await errorMessage(response));
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    const events = buffer.split("\n\n");
    buffer = events.pop() ?? "";
    for (const block of events) {
      const event = block
        .split("\n")
        .find((line) => line.startsWith("event: "))
        ?.slice(7);
      const data = block
        .split("\n")
        .find((line) => line.startsWith("data: "))
        ?.slice(6);
      if (!event || !data) continue;
      const payload = JSON.parse(data) as unknown;
      if (event === "status") {
        handlers.onStatus(
          payload as { stage: string; message: string; session_id?: string },
        );
      } else if (event === "result") {
        handlers.onResult(payload as QueryResponse);
      } else if (event === "complete") {
        handlers.onComplete(payload as { query_id: string });
      } else if (event === "error") {
        throw new Error((payload as { message: string }).message);
      }
    }
    if (done) break;
  }
}

export async function streamFinalAnswer(
  queryId: string,
  handlers: FinalAnswerHandlers,
): Promise<void> {
  const response = await fetch(
    `${API_BASE}/api/v1/queries/${queryId}/answer/stream`,
    {
      headers: authHeaders(),
      cache: "no-store",
    },
  );
  if (!response.ok || !response.body) {
    throw new Error(await errorMessage(response));
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    const events = buffer.split("\n\n");
    buffer = events.pop() ?? "";
    for (const block of events) {
      const event = block
        .split("\n")
        .find((line) => line.startsWith("event: "))
        ?.slice(7);
      const data = block
        .split("\n")
        .find((line) => line.startsWith("data: "))
        ?.slice(6);
      if (!event || !data) continue;
      const payload = JSON.parse(data) as unknown;
      if (event === "answer_start") {
        handlers.onStart(
          payload as { query_id: string; mode: AnswerMode },
        );
      } else if (event === "answer_delta") {
        handlers.onDelta(
          payload as { query_id: string; content: string },
        );
      } else if (event === "answer_done") {
        handlers.onDone(
          payload as {
            query_id: string;
            mode: AnswerMode;
            answer: string;
            insight: Insight;
          },
        );
      } else if (event === "answer_error") {
        throw new Error((payload as { message: string }).message);
      }
    }
    if (done) break;
  }
}

export async function drillQuery(
  queryId: string,
  target: TimeGranularity,
  periodStart?: string,
  periodEnd?: string,
): Promise<QueryResponse> {
  const response = await fetch(`${API_BASE}/api/v1/queries/${queryId}/drill`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...authHeaders(),
    },
    body: JSON.stringify({
      target_granularity: target,
      period_start: periodStart,
      period_end: periodEnd,
    }),
  });
  if (!response.ok) throw new Error(await errorMessage(response));
  return response.json() as Promise<QueryResponse>;
}

export async function getHistory(keyword = ""): Promise<HistoryItem[]> {
  const query = keyword ? `?keyword=${encodeURIComponent(keyword)}` : "";
  const response = await fetch(`${API_BASE}/api/v1/history${query}`, {
    headers: authHeaders(),
    cache: "no-store",
  });
  if (!response.ok) throw new Error(await errorMessage(response));
  const payload = (await response.json()) as { items: HistoryItem[] };
  return payload.items;
}

export async function getHistoryDetail(
  queryId: string,
): Promise<QueryResponse> {
  const response = await fetch(`${API_BASE}/api/v1/history/${queryId}`, {
    headers: authHeaders(),
    cache: "no-store",
  });
  if (!response.ok) throw new Error(await errorMessage(response));
  return response.json() as Promise<QueryResponse>;
}

export async function getSessionHistory(
  sessionId: string,
  ownerUserId?: string,
): Promise<QueryResponse[]> {
  const parameters = new URLSearchParams();
  if (ownerUserId) parameters.set("owner_user_id", ownerUserId);
  const suffix = parameters.size ? `?${parameters}` : "";
  const response = await fetch(
    `${API_BASE}/api/v1/sessions/${encodeURIComponent(sessionId)}/history${suffix}`,
    {
      headers: authHeaders(),
      cache: "no-store",
    },
  );
  if (!response.ok) throw new Error(await errorMessage(response));
  const payload = (await response.json()) as { items: QueryResponse[] };
  return payload.items;
}

export async function downloadHistoryBatch(options: {
  scope: "all" | "recent" | "session";
  recentCount?: number;
  sessionId?: string;
  ownerUserId?: string;
}): Promise<void> {
  const parameters = new URLSearchParams({ scope: options.scope });
  if (options.recentCount) {
    parameters.set("recent_count", String(options.recentCount));
  }
  if (options.sessionId) parameters.set("session_id", options.sessionId);
  if (options.ownerUserId) parameters.set("owner_user_id", options.ownerUserId);
  const response = await fetch(
    `${API_BASE}/api/v1/history/export/batch?${parameters}`,
    { headers: authHeaders() },
  );
  if (!response.ok) throw new Error(await errorMessage(response));
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `bank-history-${new Date().toISOString().slice(0, 10)}.xlsx`;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

export async function createShare(
  queryId: string,
): Promise<{ share_id: string; token: string; expires_at: string }> {
  const response = await fetch(`${API_BASE}/api/v1/queries/${queryId}/share`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...authHeaders(),
    },
    body: JSON.stringify({ expires_hours: 24 }),
  });
  if (!response.ok) throw new Error(await errorMessage(response));
  return response.json() as Promise<{
    share_id: string;
    token: string;
    expires_at: string;
  }>;
}

export async function downloadExport(
  queryId: string,
  format: "xlsx" | "csv",
): Promise<void> {
  const response = await fetch(
    `${API_BASE}/api/v1/queries/${queryId}/export?format=${format}`,
    { headers: authHeaders() },
  );
  if (!response.ok) throw new Error(await errorMessage(response));
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `bank-query-${queryId.slice(0, 8)}.${format}`;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

export async function getSharedQuery(token: string): Promise<QueryResponse> {
  const response = await fetch(`${API_BASE}/api/v1/shares/${token}`, {
    headers: {
      "X-User-Id": "demo-viewer",
      "X-User-Role": "viewer",
    },
    cache: "no-store",
  });
  if (!response.ok) throw new Error(await errorMessage(response));
  return response.json() as Promise<QueryResponse>;
}

export async function getAdminOverview(): Promise<Record<string, number>> {
  const response = await fetch(`${API_BASE}/api/v1/admin/overview`, {
    headers: authHeaders(),
    cache: "no-store",
  });
  if (!response.ok) throw new Error(await errorMessage(response));
  return response.json() as Promise<Record<string, number>>;
}

export async function getAudit(
  sort: "newest" | "risk_desc" | "risk_asc" = "newest",
  risk: "all" | "elevated" | "low" | "medium" | "high" = "all",
  user = "",
): Promise<AuditItem[]> {
  const parameters = new URLSearchParams({ sort, risk });
  if (user) parameters.set("user", user);
  const response = await fetch(`${API_BASE}/api/v1/admin/audit?${parameters}`, {
    headers: authHeaders(),
    cache: "no-store",
  });
  if (!response.ok) throw new Error(await errorMessage(response));
  const payload = (await response.json()) as { items: AuditItem[] };
  return payload.items;
}

export async function getAdminUsers(): Promise<AdminUserSummary[]> {
  const response = await fetch(`${API_BASE}/api/v1/admin/users`, {
    headers: authHeaders(),
    cache: "no-store",
  });
  if (!response.ok) throw new Error(await errorMessage(response));
  const payload = (await response.json()) as { items: AdminUserSummary[] };
  return payload.items;
}

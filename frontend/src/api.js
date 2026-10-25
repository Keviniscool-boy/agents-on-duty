export const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";
const TOKEN_KEY = "agent_token";

export function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token) {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

export async function api(path, options = {}) {
  const token = getToken();
  const response = await fetch(`${API_URL}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.headers || {}),
    },
  });
  const data = await response.json().catch(() => ({}));
  if (response.status === 401) {
    clearToken();
    window.dispatchEvent(new Event("auth-expired"));
  }
  if (!response.ok) throw new Error(data.detail || "请求失败，请稍后重试");
  return data;
}

export const INTENT_LABELS = {
  knowledge_rag: "知识库",
  ticket_handler: "业务办理",
  fallback: "兜底应答",
};

export const INTENT_CLASS = {
  knowledge_rag: "info",
  ticket_handler: "action",
  fallback: "warn",
};

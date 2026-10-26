export const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";

// B10：token 已迁移到 httpOnly Cookie（后端 Set-Cookie），
// 前端不再读写 localStorage；跨端口请求需 credentials: "include"。
export async function api(path, options = {}) {
  const response = await fetch(`${API_URL}${path}`, {
    ...options,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  const data = await response.json().catch(() => ({}));
  if (response.status === 401) {
    window.dispatchEvent(new Event("auth-expired"));
  }
  if (!response.ok) throw new Error(data.detail || "请求失败，请稍后重试");
  return data;
}

export async function logout() {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch {
    // Cookie 已随会话失效，忽略登出接口异常
  }
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

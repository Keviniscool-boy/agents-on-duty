import { useState } from "react";
import { api } from "./api.js";

export default function AuthView({ onLogin }) {
  const [registerMode, setRegisterMode] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      const data = await api(`/api/auth/${registerMode ? "register" : "login"}`, {
        method: "POST",
        body: JSON.stringify({ username: username.trim(), password }),
      });
      onLogin(data.user);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="auth-shell">
      <section className="auth-panel">
        <div className="brand-mark">SC</div>
        <p className="eyebrow">SMART CUSTOMER SERVICE</p>
        <h1>{registerMode ? "创建账户" : "欢迎回来"}</h1>
        <p className="muted">登录后开始使用智能客服 Agent</p>
        <form onSubmit={submit}>
          <label>
            用户名
            <input
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              minLength="3"
              required
              autoComplete="username"
            />
          </label>
          <label>
            密码
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              minLength="6"
              required
              autoComplete={registerMode ? "new-password" : "current-password"}
            />
          </label>
          {error && <p className="error">{error}</p>}
          <button className="primary" disabled={busy}>
            {busy ? "处理中..." : registerMode ? "注册并登录" : "登录"}
          </button>
        </form>
        <button
          className="text-button"
          onClick={() => {
            setRegisterMode(!registerMode);
            setError("");
          }}
        >
          {registerMode ? "已有账户？返回登录" : "还没有账户？立即注册"}
        </button>
      </section>
    </main>
  );
}

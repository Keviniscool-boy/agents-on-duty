import { useCallback, useEffect, useState } from "react";
import AuthView from "./AuthView.jsx";
import ChatView from "./ChatView.jsx";
import { api, clearToken, getToken } from "./api.js";

export default function App() {
  const [user, setUser] = useState(null);
  const logout = useCallback(() => {
    clearToken();
    setUser(null);
  }, []);

  useEffect(() => {
    if (getToken()) {
      api("/api/auth/me")
        .then((data) => setUser(data.user))
        .catch(logout);
    }
  }, [logout]);

  return user ? <ChatView user={user} onLogout={logout} /> : <AuthView onLogin={setUser} />;
}

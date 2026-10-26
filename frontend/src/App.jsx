import { useCallback, useEffect, useState } from "react";
import AuthView from "./AuthView.jsx";
import ChatView from "./ChatView.jsx";
import { api } from "./api.js";

function App() {
  const [user, setUser] = useState(null);
  const logout = useCallback(() => {
    setUser(null);
  }, []);

  useEffect(() => {
    // B10：登录态由 httpOnly Cookie 承载，进入时向服务端确认。
    api("/api/auth/me")
      .then((data) => setUser(data.user))
      .catch(() => setUser(null));
  }, [logout]);

  return user ? <ChatView user={user} onLogout={logout} /> : <AuthView onLogin={setUser} />;
}

export default App;

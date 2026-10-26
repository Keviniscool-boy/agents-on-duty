import os
import uuid

from locust import HttpUser, between, task


class CustomerServiceUser(HttpUser):
    """模拟一个独立登录并连续使用客服系统的用户。"""

    wait_time = between(2, 5)

    def on_start(self):
        username = f"load_{uuid.uuid4().hex[:10]}"
        password = os.getenv("LOADTEST_PASSWORD", "")
        register = self.client.post(
            "/api/auth/register",
            json={"username": username, "password": password},
            name="POST /api/auth/register",
        )
        if register.status_code not in (200, 201):
            register.failure(f"register failed: {register.status_code}")
            return

        data = register.json()
        self.token = data["access_token"]
        self.session_id = None
        self.headers = {"Authorization": f"Bearer {self.token}"}

    @task(4)
    def chat(self):
        if not getattr(self, "token", None):
            return

        response = self.client.post(
            "/api/chat",
            headers=self.headers,
            json={
                "message": "商品签收后几天内可以申请退货？",
                "session_id": self.session_id,
            },
            name="POST /api/chat",
        )
        if response.status_code == 200:
            self.session_id = response.json().get("session_id", self.session_id)
        elif response.status_code != 503:
            response.failure(f"chat failed: {response.status_code}")

    @task(1)
    def history(self):
        if not getattr(self, "session_id", None):
            return

        response = self.client.get(
            f"/api/history/{self.session_id}",
            headers=self.headers,
            name="GET /api/history/{session_id}",
        )
        if response.status_code != 200:
            response.failure(f"history failed: {response.status_code}")

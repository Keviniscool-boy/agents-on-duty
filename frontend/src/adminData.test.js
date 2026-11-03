import test from "node:test";
import assert from "node:assert/strict";

import { createRequestGuard, resolveRows } from "./adminData.js";

test("概览响应中的 users 计数对象不会被当作账号列表渲染", () => {
  const overview = {
    by_status: { pending_review: 1 },
    users: { user: 3, agent: 1, admin: 1 },
  };
  assert.deepEqual(resolveRows(overview, "users"), []);
});

test("账号页返回数组时正常渲染", () => {
  const users = { users: [{ id: "u1", username: "alice", role: "user" }] };
  assert.equal(resolveRows(users, "users").length, 1);
});

test("审计页回退到 events 数组", () => {
  const events = { events: [{ id: 1, action: "role_changed" }] };
  assert.equal(resolveRows(events, "audit").length, 1);
});

test("空响应安全返回空数组", () => {
  assert.deepEqual(resolveRows(null, "users"), []);
  assert.deepEqual(resolveRows({}, "refunds"), []);
});

test("快速切换时旧请求不会覆盖新请求", () => {
  const guard = createRequestGuard();
  const overviewRequest = guard.begin();
  const usersRequest = guard.begin();
  assert.equal(guard.isCurrent(overviewRequest), false);
  assert.equal(guard.isCurrent(usersRequest), true);
  // 旧响应晚到：仍被判定为非当前请求
  assert.equal(guard.isCurrent(overviewRequest), false);
});

# 02 · API 接口文档

Base URL：`http://<host>:8000` · 所有业务接口要求登录（见 §0） · 错误统一 `{"detail": ...}`

## 0. 认证

| 方式 | 说明 |
|---|---|
| httpOnly Cookie | 登录后自动下发 `agent_token`（SameSite=Strict，生产环境 Secure） |
| Bearer | `Authorization: Bearer <JWT>`，供脚本/评测使用 |

- 令牌携带 `jti`，登出即吊销（Cookie 与 Bearer 同时存在时两者都吊销）。
- 角色实时读库：改角色/封禁立即生效，不等 token 过期。
- 登录限流：同 IP+用户名 60 秒窗口内最多 5 次，超限 429。

## 1. 认证接口

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | `/api/auth/register` | 无 | 用户名≥3 字符、密码≥6 位；201 返回 token+用户 |
| POST | `/api/auth/login` | 无 | 200 返回 token+用户；401 用户名或密码错误；429 限流 |
| POST | `/api/auth/logout` | 可选 | 吊销 Bearer/Cookie 携带的令牌并清 Cookie |
| GET | `/api/auth/me` | 登录 | 当前用户（含实时角色） |

## 2. 聊天

### POST /api/chat（同步）· 登录
```json
// 请求
{ "message": "查询我的订单", "session_id": "可选，缺省新建" }
// 200
{
  "response": "…最终答复…",
  "session_id": "…",
  "intent": "ticket_handler",
  "current_agent": "ticket_handler",
  "compliance_passed": true,
  "compliance_reason": "",
  "awaiting_human": false,
  "knowledge_decision": "",
  "handoff_required": false,
  "order_cards": [ { "order_id": "…", "status": "delivered", "amount_cents": 29900, "product": "…", "delivered_at": "…" } ],
  "trace_id": "…"
}
```
- `order_cards`：仅当本轮调用了订单工具，携带**工具返回的结构化结果**；无结构化数据时为空数组（前端保持纯文本，不从自然语言猜金额）。
- 错误：409 会话归属冲突 / 等待人工审核 / 轮次占用；500 明确报"Agent 处理失败，请稍后重试"（不伪装成功）。

### POST /api/chat/stream（SSE，推荐）· 登录
事件流（`data: {JSON}\n\n`）：

| event | 载荷 | 说明 |
|---|---|---|
| session | `{"session_id"}` | 建立会话 |
| status | `{"node","message"}` | 节点级阶段（中文，如"正在查询知识库"） |
| final | ChatResponse 全量 | 最终答复（含 order_cards / trace_id） |
| error | `{"message"}` | 处理失败/超时；**客户端保留用户输入并提供重试** |

整体 120s 服务端兜底超时；前端另有连接期 20s / 流期静默 90s 看门狗。

### GET /api/history/{session_id} · 登录
会话消息 + 售后通知按时间合并；仅会话归属人可读（403）。

### GET /api/sessions · 登录
当前用户会话列表（含首条消息截断标题）。

## 3. 订单与售后

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/api/orders` | 登录 | 本人演示订单（每账号自动配 2 单：已签收/已取消） |
| GET | `/api/orders/{id}` | 登录 | 单笔订单；404 不存在或非本人 |
| GET | `/api/refunds` | 登录 | 本人退款申请列表 |
| POST | `/api/refunds` | 登录 | 提交退款；Header `Idempotency-Key` 必填；409 重复/冲突；404 订单不存在 |
| POST | `/api/refunds/{id}/cancel` | 登录 | 撤回（若有关联暂停线程则触发恢复，`resume_pending` 表示恢复待重试） |
| POST | `/api/refunds/{id}/information` | 登录 | 客服要求补充信息时提交（工单 processing 状态） |

退款状态机：`pending_review → approved / rejected / completed / cancelled`；
同一订单仅允许一笔活跃申请（部分唯一索引强制）。

## 4. 工单（转人工闭环）

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/api/tickets` | 登录 | **仅本人的**工单；`?limit≤100&offset≥0`；稳定排序（创建时间倒序）；返回 `{"tickets":[...], "total":n}` |
| GET | `/api/workbench/tickets` | agent/admin | 工作台队列（全量，`?status=` 过滤） |
| POST | `/api/workbench/tickets/{id}/status` | agent/admin | 状态只能改为 `processing` / `resolved`；记录操作者 |

工单字段：`ticket_id / title / description / status(pending|processing|resolved|…) / category / priority / user_id / username / session_id / created_at / handled_by / handled_by_id / updated_at`。

### 工单对话（转人工闭环的沟通通道）

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/api/tickets/{id}` | 登录（工单归属人） | 工单详情 + 全部对话（403 非本人 / 404 不存在） |
| POST | `/api/tickets/{id}/messages` | 登录（工单归属人） | 用户追问/补充，`{content}` 1-2000 字 |
| GET | `/api/workbench/tickets/{id}/messages` | agent/admin | 客服读取任意工单对话 |
| POST | `/api/workbench/tickets/{id}/messages` | agent/admin | 客服回复；用户在"我的工单"可见 |

消息字段：`id / sender_role(user|agent) / sender_name / content / created_at`。

确定性转人工：聊天中发送"转人工 / 人工客服 / 找人工 / 转接人工"等表达 → 系统直接建单
（不经模型），答复含工单号与当前状态；同会话重试返回原单（幂等）。

## 5. 知识服务

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | `/api/knowledge/search` | 登录 | `{query, top_k≤?, min_score}`；命中带 source/content/score；**降级态 503** `knowledge_unavailable` |
| GET | `/api/knowledge/status` | 登录 | 检索服务状态 |

## 6. 运维接口（agent/admin，普通用户 403）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/tools` | MCP 工具清单 |
| POST | `/api/tools/call` | 直调白名单工具；`user_id/session_id/idempotency_key` 服务端注入，正文伪造无效 |
| GET | `/api/metrics` | Agent 指标汇总（进程内存态，重启归零） |

## 7. 工作台（agent/admin）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/workbench/refunds` | 退款审核队列 |
| GET | `/api/workbench/refunds/{id}` | 详情：申请+订单+审计+暂停线程交接单+会话历史+操作记录 |
| POST | `/api/workbench/refunds/{id}/decision` | `approve / reject / need_info`（need_info 必填 note）→ 触发图恢复 |
| POST | `/api/workbench/refunds/{id}/resume` | 手动重试恢复（租约过期/失败后） |
| POST | `/api/workbench/refunds/{id}/prepare` | 重建"建单后、暂停前"崩溃检查点的人工审核暂停 |

## 8. 管理端（admin）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/overview` | 退款状态分布、待处理/失败工单、账号分布 |
| GET | `/api/admin/users` | 账号列表（`role` 过滤、分页） |
| POST | `/api/admin/users/role` | 授予/变更角色（user/agent/admin）；不能移除最后一名管理员（409） |
| GET | `/api/admin/refunds` | 全量退款（分页/状态过滤） |
| GET | `/api/admin/runs` · `/api/admin/runs/{trace_id}` | 运行记录与单次 trace 明细 |
| GET | `/api/admin/audit` | 审计事件（`kind=role/compliance/node/tool/model/business`） |

## 9. 健康检查

GET `/health`（无鉴权）→ `{"status":"healthy|degraded|unknown","version":"1.0.0","knowledge_base":{...}}`

## 10. 错误码约定

| 码 | 语义 |
|---|---|
| 401 | 未登录 / 令牌过期或已吊销（前端广播 auth-expired 引导重登） |
| 403 | 已登录但越权（角色不符 / 会话不属于当前用户） |
| 404 | 资源不存在或非本人（不区分泄露存在性） |
| 409 | 状态冲突（重复提交 / 会话占用 / 等待人工审核 / 最后管理员） |
| 429 | 登录限流 |
| 500 | 明确的失败文案 + trace_id 可查；**绝不把失败包装成 200** |
| 503 | 依赖暂不可用（系统初始化中 / 知识库降级 / 恢复流程暂不可用） |

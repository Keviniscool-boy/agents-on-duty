# 你的第一个多 Agent 项目

> 智能客服系统：Supervisor 编排 + 合规修复回路 + 三层记忆三级降级 · FastAPI + LangGraph + MCP

一个用 **FastAPI + LangGraph** 构建的智能客服系统，目标是做一个**简单、易读、贴近生产实践**的多 Agent 入门项目：没有炫技的抽象，每个模块都对应真实业务里会遇到的工程问题——路由、合规、幂等、降级、鉴权、可观测。

## 它能做什么

用户用自然语言提问，系统自动路由到对应的 Agent 处理：

- 📚 **知识库问答**（knowledge_rag）—— 退货政策、开户流程、理财知识，基于 FAISS 向量检索回答
- 🎫 **业务办理**（ticket_handler）—— 查询订单物流、创建工单，通过 **MCP 协议**调用外部工具
- 🛡️ **合规检查**（compliance_checker）—— 规则 + LLM 双重审查，拦截隐私泄露、收益承诺、提示词泄露
- 🔄 **合规修复回路** —— 不合格的回答会带着违规原因**重写**（最多 2 次），重试耗尽自动降级为兜底话术
- 💬 **兜底应答**（fallback）—— 无关问题、危险请求、索取系统内部信息的一律礼貌拒绝

## 架构总览

```
                          ┌─────────────────────────────────────────┐
                          │              LangGraph 状态机            │
 用户消息 ──► supervisor ──► │  ┌──────────┐                           │
             (意图路由)     │  │knowledge │──┐                        │
                          │  │  _rag    │  │   ┌─────────────┐      │
                          │  ├──────────┤  ├──►│ compliance  │      │
                          │  │ ticket   │──┤   │ _checker    │      │
                          │  │ handler  │  │   └──────┬──────┘      │
                          │  ├──────────┤  │    通过 ↙   ✗ ↘        │
                          │  │ fallback │──┘  synthesize  rewrite     │
                          │  └──────────┘      (END)    │≤2次──────┘
                          └─────────────────────────────────┘
                                                  └── 重写后回到合规检查
```

## 三层记忆与三级降级

这是本项目最值得学习的部分：**每一层数据都有明确的归属和降级路径**。

| 数据 | 第一选择 | 兜底 | 说明 |
|---|---|---|---|
| 对话历史 | Redis（30分钟TTL） | MySQL → 进程内 | 短期记忆，含 user_id/session_id |
| 路由/合规上下文 | Redis（24h） | MySQL → 进程内 | 工作记忆 |
| 用户账户、会话归属 | SQLite | — | PBKDF2 密码哈希 + JWT 鉴权 |
| LangGraph 断点 | AsyncSqliteSaver | 进程内 | 重启可恢复（含重写中途状态） |
| 工单幂等记录 | SQLite | — | 语义化幂等键，重复提交返回原单号 |
| 订单/物流 | **不存储**，MCP 实时查询 | — | 订单数据留在订单系统 |

- **MySQL 是硬依赖**：启动自检失败直接拒绝启动（保底数据源）
- **Redis 挂了只是变慢**：自动降级 MySQL，服务不中断
- MySQL 里所有上下文都带 `user_id`，可审计

## 快速开始

```bash
# 1. 配置环境变量
cp .env.example .env
#    至少填写：OPENAI_API_KEY / OPENAI_BASE_URL / MODEL_NAME
#    以及 MYSQL_PASSWORD（docker compose 会用它初始化 MySQL）

# 2. 一键启动（MySQL + Redis + 后端）
docker compose up --build

# 3. 前端
cd frontend && npm install && npm run dev
#    打开 http://localhost:5173，注册账号即可对话
```

手动部署（不用 Docker）见 [Dockerfile](Dockerfile) 和 `.env.example` 中的逐项注释。

## 目录结构

```
agents/          5 个 Agent：supervisor 路由、knowledge_rag 检索问答、
                 ticket_handler 业务办理、compliance_checker 合规、
                 synthesizer 合成 + rewrite 重写节点
api/             FastAPI：/api/chat（同步）、/api/chat/stream（SSE 流式）、
                 /api/auth/*（JWT 注册登录）、/api/tools、/api/metrics
memory/          三层记忆：short_term（对话）、working_memory（路由状态）、
                 long_term（FAISS 向量知识库）
storage/         MySQL 兜底存储层（working context / history / messages）
mcp_client/      MCP 客户端，含网络重试与本地降级
mcp_tools/       MCP Server：order_query / ticket_create / risk_check
tools/           本地备用工具 + 工单幂等存储（SQLite）
auth/            JWT 签发校验 + SQLite 用户库
tracing/         OpenTelemetry 配置 + Agent 调用指标
frontend/        React + Vite：会话侧栏、SSE 实时进度、Agent 轨迹标签、运维面板
evaluation/      8 个评测脚本：路由/RAG/合规/MCP/记忆/鉴权 + locust 压测
```

## 值得细看的工程细节

| 想了解 | 去看 |
|---|---|
| 合规修复回路怎么实现 | `agents/supervisor.py` 的 `RewriteNode` + `route_after_compliance` |
| 幂等键为什么用内容指纹而不是请求 ID | `agents/ticket_handler.py` 的 `build_idempotency_key` |
| MCP 挂了怎么降级到本地工具 | `agents/ticket_handler.py` 的 `_call_with_retry` + `_local_tool` |
| 三级降级链怎么写才不乱 | `memory/working_memory.py`、`memory/short_term.py` |
| SSE 流式怎么跟 LangGraph 结合 | `api/main.py` 的 `chat_stream`（astream + stream_mode="updates"）|
| 并发下幂等怎么保证 | `tools/ticket_store.py`（主键 + IntegrityError 回查）|

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest
```

17 个用例覆盖：合规回路（图级，FakeLLM 驱动）、工单幂等、记忆降级链、登录限流、
MySQL 存储读写。`evaluation/` 下的脚本则是对着运行中的服务做端到端评测。

## 技术栈

Python 3.12 · FastAPI · LangGraph · LangChain · MCP · MySQL · Redis · SQLite ·
FAISS · React · Vite · JWT · OpenTelemetry · Docker Compose

---

> 学习路线建议：先读 `agents/supervisor.py`（60 行就能看懂整张图），
> 再看 `memory/` 理解三级降级，最后看 `api/main.py` 把链路串起来。

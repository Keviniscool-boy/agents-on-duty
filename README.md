# 你的第一个多 Agent 项目

<p align="center">
  <a href="https://github.com/Keviniscool-boy/smart-cs-multi-agent/actions/workflows/ci.yml"><img src="https://github.com/Keviniscool-boy/smart-cs-multi-agent/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/python-3.12%2B-blue" alt="Python">
  <img src="https://img.shields.io/badge/LangGraph-%E2%9C%93-teal" alt="LangGraph">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License"></a>
</p>

<p align="center">
简体中文 | <a href="README.en.md">English</a>
</p>

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

```mermaid
flowchart TD
    U["用户消息"] --> S{"supervisor_route<br/>意图路由"}
    S -->|"知识库问题"| K["knowledge_rag<br/>FAISS 检索问答"]
    S -->|"订单 / 工单"| T["ticket_handler<br/>MCP 工具调用"]
    S -->|"无关 / 危险"| F["fallback<br/>兜底应答"]
    K --> C{"compliance_checker<br/>规则 + LLM 合规检查"}
    T --> C
    F --> C
    C -->|"✓ 通过"| SYN["synthesize<br/>合成最终回答"]
    C -->|"✗ 不合规，重试 < 2 次"| R["rewrite<br/>带违规原因重写"]
    R --> C
    C -->|"重试耗尽 → 降级话术"| SYN
    SYN --> E(["END"])

    style R fill:#fdf6e7,stroke:#92600a
    style C fill:#eef3f8,stroke:#2f5d8a
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

# 2. 一键启动全栈（MySQL + Redis + 后端 + 前端）
docker compose up --build

# 3. 浏览器打开 http://localhost:8080，注册账号即可对话
```

不用 Docker 的手动部署见 [Dockerfile](Dockerfile) 和 `.env.example` 中的逐项注释；
前端单独开发时在 `frontend/` 下 `npm install && npm run dev`（访问 http://localhost:5173）。

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

## 设计决策 FAQ

**Q：工单幂等键为什么用内容指纹（用户+标题+描述哈希），而不是请求 ID？**
请求 ID 每次工具调用都会变，防得住"网络重试"，防不住"用户再说一遍"。内容指纹让同一用户重复提交相同工单时直接返回原单号——对客服场景来说，"您已有工单 TK-xxx 在处理中"本来就是更好的用户体验。代价是用户无法提第二张内容完全相同的工单，这是有意取舍。见 `agents/ticket_handler.py` 的 `build_idempotency_key`。

**Q：为什么 MySQL 是硬依赖（连不上拒绝启动），Redis 却允许挂？**
分工不同：Redis 是热缓存，挂了降级 MySQL 只是变慢，不丢数据；MySQL 是兜底数据源，它挂了意味着所有上下文无处落地，与其带病运行不如快速失败——这也是为什么启动时先 `self_check()` 再初始化其他组件。

**Q：LangGraph 的 checkpointer 为什么必须用 AsyncSqliteSaver？**
一个真实的坑：所有 API 都走 `ainvoke`/`astream`，而同步的 `SqliteSaver` 不支持 async 方法，编译能过、第一次聊天就 `NotImplementedError`。教训是**图能 compile ≠ 能跑**，切换 checkpointer 后必须用图级测试真正执行一遍（见 `test_compliance_loop.py`）。

**Q：订单/物流数据为什么在系统里不落库？**
客服系统只做透传：查询时通过 MCP 调订单系统，结果进回答就丢弃。订单数据的唯一事实源在订单系统，客服侧缓存它只会制造一致性问题。

**Q：流式为什么用 SSE 而不是 WebSocket？**
客服对话是单向推送为主（服务端 → 客户端的事件流），不需要双工。SSE 基于 HTTP、自动重连、实现简单，`graph.astream(stream_mode="updates")` 天然就是一个事件生成器，两者正好对上。

**Q：合规检查为什么是"规则先行，LLM 补充"？**
正则查 PII、敏感词是确定性的：零成本、零误判、毫秒级返回；LLM 只在规则放行后做语义层面的深查（如变相收益承诺）。规则判高危时直接短路，连 LLM 都不用调。见 `agents/compliance_checker.py` 的 `full_check`。

## 已知限制 / Roadmap

坦诚说，这个项目还有这些没做完（也正好是练手方向）：

- [ ] `order_query` 返回 mock 数据——接真实订单系统（或做一个可配置的假数据服务）
- [ ] Agent 调用指标还在进程内——多 worker 不互通、重启清零，应落 MySQL
- [ ] JWT 无登出/吊销机制——登出只删前端 token，签发的 token 到期前一直有效
- [ ] MySQL 侧数据无生命周期管理——对话消息、checkpoint 无限增长，需要 TTL 清理
- [ ] 3 个早期测试是脚本风格（import 即执行）——待改写成 pytest 用例
- [ ] 无前端截图/GIF 演示——等系统跑起来补

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

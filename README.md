<div align="center">

# 🧑‍💼 你的第一个多 Agent 项目

_一个能讲清楚每个设计决策的智能客服系统 —— LangGraph 编排 · 合规修复回路 · 三层记忆三级降级_

[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org)
[![LangGraph](https://img.shields.io/badge/LangGraph-orchestration-77C5A3)](https://langchain-ai.github.io/langgraph/)
[![React](https://img.shields.io/badge/react-19-61dafb)](https://react.dev)
[![Docker](https://img.shields.io/badge/docker-compose-2496ED)](https://docs.docker.com/compose/)

[项目介绍](docs/project-intro.zh.md) · [快速开始](#-快速开始) · [系统架构](#-系统架构) · [核心特性](#-为什么值得看) · [设计决策 FAQ](#-设计决策-faq)

**简体中文** | [English](README.en.md)

</div>

---

## 📖 项目介绍

这是一个模拟真实客服场景的完整多 Agent 系统：Supervisor 按意图把请求分派给知识库检索（FAISS RAG）、业务办理（MCP 工具）或兜底应答，每个回答出站前都要过「规则 + LLM」双重合规审查，不合规的回答带着违规原因自动重写，重试耗尽则降级为固定话术。三层记忆各自带降级路径，JWT、限流、幂等、SSE 流式、可观测性一应俱全——定位是**学习项目**，但用的全是生产模式。

> [!TIP]
> **想深挖这个项目？** 完整版介绍在 [docs/project-intro.zh.md](docs/project-intro.zh.md)：
> 一条消息的 8 步完整旅程（带代码位置）· 技术选型对照表 · **面试官视角的 22 个问题与答题要点** · 三分钟开场话术。
> 如果你是面试官，里面就是你会问的全部问题。

## 🎯 为什么值得看

网上大多 Agent 教程止步于「跑通 demo」，这个项目把真实业务才会遇到的问题做完了——**每个模块都对应一个你在生产环境和面试里会被问到的工程问题**：

- 🧭 **Supervisor 编排** —— 意图路由带确定性快路径：高频简单请求本地正则直接分流，一个 LLM 调用都省
- 🛡️ **合规修复回路** —— 不合规回答不是简单拦截，而是带着违规原因**重写**（≤2 次），耗尽自动降级兜底话术
- 🪆 **三级降级链** —— Redis 挂了走 MySQL，MySQL 挂了走进程内；而 MySQL 本身是硬依赖，启动自检失败拒绝启动——**每个组件的失效语义都想清楚了**
- 🎫 **语义化幂等** —— 工单幂等键用「用户 + 内容指纹」而非请求 ID，防得住网络重试，也防得住"用户再说一遍"
- 🔌 **MCP 工具调用** —— 标准协议接入订单系统，网络异常有限重试 + 本地工具降级，两边共享幂等存储
- 📡 **SSE 实时流式** —— `graph.astream(stream_mode="updates")` 直接映射为事件流，前端能看到 Agent 一步一步干活
- 🔐 **完整安全栈** —— JWT 鉴权、PBKDF2 密码哈希、登录限流、会话归属校验、PII 自动脱敏
- 📊 **可观测性** —— OpenTelemetry 追踪 + 每个 Agent 的调用指标，前端自带运维面板

## 🚀 快速开始

> [!TIP]
> 全栈一键启动（MySQL + Redis + 后端 + 前端），只需要 Docker 和一个 LLM API Key。

```bash
# 1. 配置环境变量
git clone https://github.com/Keviniscool-boy/agents-on-duty.git
cd agents-on-duty
cp .env.example .env
#    编辑 .env，至少填写：
#    OPENAI_API_KEY / OPENAI_BASE_URL / MODEL_NAME
#    以及 MYSQL_PASSWORD（compose 会用它初始化 MySQL）

# 2. 一键启动
docker compose up --build -d

# 3. 浏览器打开 http://localhost:8080，注册账号，开始对话 ✅
```

试试这三个典型问题，观察回复下方的 Agent 轨迹标签：

| 你问 | 谁来接 | 你会看到 |
|---|---|---|
| 「商品签收后几天内可以退货？」 | 📚 知识库 RAG | FAISS 检索 → 合规通过 |
| 「查询订单 ORD-20260401-001 的物流」 | 🎫 业务办理 | MCP 工具调用 → 合规通过 |
| 「写一段小说」 | 💬 兜底应答 | 礼貌拒绝 |

<details>
<summary><b>不用 Docker，手动部署</b></summary>

```bash
# 后端（Python 3.12+，需要本机 MySQL 与 Redis）
pip install -r requirements-dev.txt
uvicorn api.main:app --port 8000

# 前端
cd frontend && npm install && npm run dev   # http://localhost:5173
```

环境变量逐项说明见 `.env.example`。
</details>

## 🏗️ 系统架构

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

## 🧠 三层记忆 · 三级降级

> [!NOTE]
> 这是本项目最值得细读的部分：**每一份数据都有明确的归属和失效语义**。

| 数据 | 第一选择 | 兜底 | 寿命 |
|---|---|---|---|
| 对话历史 | Redis（30 min TTL） | MySQL → 进程内 | 短期 |
| 路由 / 合规上下文 | Redis（24 h TTL） | MySQL → 进程内 | 会话级 |
| 用户账户、会话归属 | SQLite | — | 永久 |
| LangGraph 断点 | AsyncSqliteSaver | 进程内 | 永久，重启可恢复 |
| 工单幂等记录 | SQLite | — | 永久 |
| 订单 / 物流 | **不落库**，MCP 实时查询 | — | 数据留在订单系统 |

- MySQL 是兜底数据源 → **硬依赖**，启动自检失败直接拒绝启动
- Redis 是热缓存 → 挂了**只是变慢**，服务不中断
- 所有落 MySQL 的上下文都带 `user_id` / `session_id`，可审计

## 🗂️ 项目结构

```
agents/          5 个 Agent + rewrite 重写节点
api/             FastAPI：同步 chat、SSE 流式、JWT 鉴权、工具与指标
memory/          short_term（对话）/ working_memory（路由状态）/ long_term（FAISS）
storage/         MySQL 兜底存储层
mcp_client/      MCP 客户端：重试 + 本地降级
mcp_tools/       MCP Server：order_query / ticket_create / risk_check
tools/           本地备用工具 + 工单幂等存储
auth/            JWT + SQLite 用户库
tracing/         OpenTelemetry + Agent 指标
frontend/        React + Vite：会话管理、实时进度、轨迹标签、运维面板
evaluation/      8 个端到端评测脚本 + locust 压测
```

## ❓ 设计决策 FAQ

<details open>
<summary><b>工单幂等键为什么用内容指纹，而不是请求 ID？</b></summary>
<br>
请求 ID 每次工具调用都变——防得住网络重试，防不住"用户再说一遍"。内容指纹让重复提交直接返回原单号，对客服场景这本来就是更好的体验。代价是同内容工单只收一次，这是有意取舍。
<br><br><code>agents/ticket_handler.py → build_idempotency_key</code>
</details>

<details>
<summary><b>为什么 MySQL 是硬依赖，Redis 却允许挂？</b></summary>
<br>
分工不同：Redis 是热缓存，挂了降级 MySQL 只是变慢；MySQL 是兜底数据源，它挂了意味着所有上下文无处落地——与其带病运行不如快速失败。
<br><br><code>storage/mysql_store.py → self_check</code>
</details>

<details>
<summary><b>LangGraph 的 checkpointer 为什么必须用 AsyncSqliteSaver？</b></summary>
<br>
一个真实的坑：所有 API 走 <code>ainvoke</code>/<code>astream</code>，同步的 SqliteSaver 不支持 async 方法——图能编译，第一次聊天就 <code>NotImplementedError</code>。教训：<b>图能 compile ≠ 能跑</b>，切 checkpointer 后必须用图级测试真正执行（见 <code>test_compliance_loop.py</code>）。
</details>

<details>
<summary><b>订单数据为什么不在系统里落库？</b></summary>
<br>
客服系统只做透传：MCP 查询 → 写进回答 → 用完即弃。订单的唯一事实源在订单系统，缓存它只会制造一致性问题。
</details>

<details>
<summary><b>流式为什么选 SSE 而不是 WebSocket？</b></summary>
<br>
客服对话以服务端单向推送为主。SSE 基于普通 HTTP、自动重连，而 <code>graph.astream(stream_mode="updates")</code> 天然就是事件生成器，两者严丝合缝。
</details>

<details>
<summary><b>合规检查为什么"规则先行，LLM 补充"？</b></summary>
<br>
正则查 PII、敏感词是确定性的：零成本、零误判、毫秒级；LLM 只在规则放行后做语义深查。规则判高危直接短路，连 LLM 调用都省掉。
<br><br><code>agents/compliance_checker.py → full_check</code>
</details>

## ✅ 测试

```bash
pip install -r requirements-dev.txt
python -m pytest          # 21 passed
```

**最近一次全量结果：21 passed / 0 failed（2026-10-25，Python 3.12 · Windows，37.5s），CI 每次推送全量复跑。**

| 模块 | 用例 | 验证什么 | 结果 |
|---|---|---|---|
| 合规修复回路 | 3 | 图级测试（FakeLLM 驱动完整 LangGraph）：一次通过、重写 2 次耗尽后降级、重写后通过 | ✅ |
| 工单幂等与限流 | 7 | 语义幂等键命中/隔离、LLM 多传参数白名单、working memory 降级、登录限流 429、AsyncSqliteSaver 创建关闭 | ✅ |
| MCP 弹性 | 3 | 本地工具与 MCP 共享幂等存储、MCP 建单幂等 | ✅ |
| MySQL 兜底与降级链 | 4 | 自检快速失败、Redis+MySQL 双挂时降级进程内、**真实 MySQL 读写往返** | ✅ |
| 工作记忆 | 4 | 上下文读写、历史截断、会话清理、会话隔离 | ✅ |

> [!NOTE]
> MySQL 相关用例在服务不可达时自动跳过或走降级路径，CI 上不需要真实数据库也能全绿；
> 本机有 MySQL 时（如本次）roundtrip 用例会对真实库做完整读写验证。

<details>
<summary><b>端到端评测</b></summary>

`evaluation/` 下 8 个脚本对着运行中的服务实测：路由准确率、RAG 质量、合规拦截、
MCP 弹性、记忆一致性、鉴权安全，以及 locust 压测。用法见各脚本头部注释。
</details>

## 🛠️ 技术栈

| 层 | 技术 |
|---|---|
| 编排 | LangGraph · LangChain |
| 服务 | FastAPI · Uvicorn · SSE |
| 工具协议 | MCP (Model Context Protocol) |
| 存储 | MySQL 8 · Redis · SQLite · FAISS |
| 前端 | React 19 · Vite · 原生 SSE |
| 可观测 | OpenTelemetry · 自研 Agent 指标 |
| 质量 | Pytest · GitHub Actions · Locust |
| 部署 | Docker Compose · Nginx |

## 📈 真实评测记录

开发过程中对每个子系统做过参数化调优并留有[完整评测日报](docs/test-record.md)（2026-09-11），关键数字：

| 子系统 | 调优动作 | 调优前 | 调优后 |
|---|---|---|---|
| 意图路由 | 新增 `fallback` 意图，兜底条件写入路由提示词 | 66.67%（8/12） | **100%**（12/12），均延迟 24.59s/条 |
| RAG 检索 | 检索阈值 0.6 → 0.55；RAG 来源传入合规检查；补高置信度本地路由 | — | 命中率 **92%**，回答正确率 **80%**（25 样本） |
| 合规检查 | 补 PII/承诺类规则；RAG 上下文传入 LLM 审查 | — | 规则 **6/6**，API 链路 **8/8** |
| MCP 工具链 | 修复评测脚本导入路径 | — | **7/7**（验证协议与调用链，非数据准确性） |
| 三层记忆 | — | — | **7/7**，含 Redis 摘除→内存降级→恢复复测 |
| 鉴权与会话 | 注册/重复注册/错误凭证/跨用户会话隔离 | — | **9/9** |

> [!NOTE]
> 这些是**开发期自建样本集**上的结果，样本量小（12–25 条），用于验证设计方向而非声称生产级准确率——评测脚本与样本集在 [evaluation/](evaluation/) 可复跑。订单数据为 mock，MCP 结果代表链路正确而非业务数据准确。

## 📄 License

[MIT](LICENSE) © 2026

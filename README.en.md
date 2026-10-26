<div align="center">

# Agents on Duty

_A customer-service system where every design decision can be explained — LangGraph orchestration · compliance repair loop · three-tier memory with graceful degradation_

[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org)
[![LangGraph](https://img.shields.io/badge/LangGraph-orchestration-77C5A3)](https://langchain-ai.github.io/langgraph/)
[![React](https://img.shields.io/badge/react-19-61dafb)](https://react.dev)
[![Docker](https://img.shields.io/badge/docker-compose-2496ED)](https://docs.docker.com/compose/)

[About](#-about-this-project) · [Quick Start](#-quick-start) · [Architecture](#-architecture) · [Highlights](#-why-its-worth-reading) · [Design FAQ](#-design-decision-faq)

[简体中文](README.md) | **English**

</div>

---

## 📖 About this project

This is a runnable multi-agent customer-service learning project. A Supervisor dispatches requests to knowledge retrieval, order/ticket tools, or fallback. Responses go through rules and LLM compliance checks; failed responses are rewritten up to twice before a fixed fallback. Order data is mocked, and the compliance checks demonstrate the architecture rather than a production policy.

Chat history and routing context prefer Redis, then MySQL, then in-process storage. The vector knowledge base uses FAISS with pluggable embeddings (DashScope native, or any OpenAI-compatible endpoint). JWT auth, login rate limiting, PII detection, ticket idempotency, SSE progress events, and OpenTelemetry metrics are also present.

The project is meant for **learning**. Start with `agents/supervisor.py`, then read `memory/` and `api/main.py`. The order and logistics tools use bundled mock data; the compliance rules and small evaluation sets need further work before production use.

## 🎯 Why it's worth reading

Most Agent tutorials stop at "the demo runs". This project finishes the problems you only meet in real business — **every module maps to an engineering question you'll be asked in production or interviews**:

- 🧭 **Supervisor orchestration** — intent routing with a deterministic fast path: common simple requests are classified by local regex, saving an LLM call entirely
- 🛡️ **Compliance repair loop** — a non-compliant answer isn't just blocked; it's **rewritten with the violation reason** (≤2 retries), then degrades to a fallback script
- 🪆 **Three-level degradation** — Redis down → MySQL; MySQL down → in-process; and MySQL itself is a hard dependency whose startup self-check refuses to boot the app — **failure semantics are explicit for every component**
- 🎫 **Semantic idempotency** — ticket idempotency keys use "user + content fingerprint" instead of request IDs, surviving both network retries and "the user asks again"
- 🔌 **MCP tool calls** — standard protocol into the order system, bounded retry on network errors + local fallback tools sharing the same idempotency store
- 📡 **SSE streaming** — `graph.astream(stream_mode="updates")` maps directly onto an event stream; the frontend watches agents work step by step
- 🔐 **Complete security stack** — JWT auth, PBKDF2 password hashing, login rate limiting, session ownership checks, automatic PII masking
- 📊 **Observability** — OpenTelemetry tracing + per-agent metrics, with an ops panel built into the frontend

## 🚀 Quick Start

> [!TIP]
> You need Docker, an OpenAI-compatible chat model, and an embedding service — set `DASHSCOPE_API_KEY` for the Aliyun-native path, or leave it empty to reuse `OPENAI_BASE_URL` with any OpenAI-compatible endpoint.

```bash
# 1. Configure the environment
git clone https://github.com/Keviniscool-boy/agents-on-duty.git
cd agents-on-duty
cp .env.example .env
#    Edit .env — at minimum:
#    OPENAI_API_KEY / OPENAI_BASE_URL / MODEL_NAME
#    DASHSCOPE_API_KEY, MYSQL_PASSWORD, and JWT_SECRET_KEY
#    Set FRONTEND_PORT=8081 if port 8080 is occupied

# 2. Start everything
docker compose up --build -d

# 3. Open http://localhost:8080 (or your FRONTEND_PORT), register, and chat
```

Try these three questions and watch the agent trace chips under each reply:

| You ask | Who answers | What you see |
|---|---|---|
| "How many days after delivery can I return an item?" | 📚 Knowledge RAG | FAISS retrieval → compliance pass |
| "Check the status of order 1001" | 🎫 Ticket handler | MCP tool call → compliance check |
| "Write me a novel" | 💬 Fallback | polite refusal |

The MCP order tool currently returns a fixed mock status for any supplied order ID. Only its local fallback uses the `1001/1002` entries in [`tools/mock_data.py`](tools/mock_data.py); neither path verifies real logistics data.

<details>
<summary><b>Manual setup without Docker</b></summary>

```bash
# Backend (Python 3.12+, needs local MySQL and Redis)
pip install -r requirements-dev.txt
uvicorn api.main:app --port 8000

# Frontend
cd frontend && npm install && npm run dev   # http://localhost:5173
```

See `.env.example` for a per-variable explanation.
</details>

## 🏗️ Architecture

```mermaid
flowchart TD
    U["User message"] --> S{"supervisor_route<br/>intent routing"}
    S -->|"knowledge"| K["knowledge_rag<br/>FAISS retrieval"]
    S -->|"order / ticket"| T["ticket_handler<br/>MCP tool calls"]
    S -->|"off-topic / unsafe"| F["fallback<br/>canned reply"]
    K --> C{"compliance_checker<br/>rules + LLM review"}
    T --> C
    F --> C
    C -->|"✓ pass"| SYN["synthesize<br/>final answer"]
    C -->|"✗ fail, retries < 2"| R["rewrite<br/>with violation reason"]
    R --> C
    C -->|"retries exhausted → degrade"| SYN
    SYN --> E(["END"])

    style R fill:#fdf6e7,stroke:#92600a
    style C fill:#eef3f8,stroke:#2f5d8a
```

## 🧠 Three-tier memory, three-level degradation

> [!NOTE]
> The most instructive part of this project: **every piece of data has an explicit owner and explicit failure semantics**.

| Data | First choice | Fallback | Lifetime |
|---|---|---|---|
| Chat history | Redis (30 min TTL) | MySQL → in-process | short-term |
| Routing / compliance context | Redis (24 h TTL) | MySQL → in-process | per-session |
| Users, session ownership | SQLite | — | permanent |
| LangGraph checkpoints | AsyncSqliteSaver | in-process | permanent, survives restarts |
| Ticket idempotency | SQLite | — | permanent |
| Orders / logistics | **not stored** — queried live via MCP | — | lives in the order system |

- MySQL is the system of record → **hard dependency**; startup self-check fails → the app refuses to boot
- Redis is a hot cache → going down **only makes it slower**, no downtime
- Everything persisted to MySQL carries `user_id` / `session_id` for auditability

## 🗂️ Project layout

```
agents/          5 agents + the rewrite node
api/             FastAPI: sync chat, SSE streaming, JWT auth, tools & metrics
memory/          short_term (chat) / working_memory (routing state) / long_term (FAISS)
storage/         MySQL fallback store
mcp_client/      MCP client: retry + local fallback
mcp_tools/       MCP server: order_query / ticket_create / risk_check
tools/           local fallback tools + ticket idempotency store
auth/            JWT + SQLite user database
tracing/         OpenTelemetry + agent metrics
frontend/        React + Vite: sessions, live progress, trace chips, ops panel
evaluation/      8 end-to-end evaluation scripts + locust load tests
```

## ❓ Design decision FAQ

<details open>
<summary><b>Why is the ticket idempotency key a content fingerprint instead of a request ID?</b></summary>
<br>
A request ID changes on every tool call — it survives network retries but not "the user asks again". A content fingerprint returns the original ticket ID on duplicates, which in customer service is better UX anyway. Same-content tickets are accepted only once — a deliberate trade-off.
<br><br><code>agents/ticket_handler.py → build_idempotency_key</code>
</details>

<details>
<summary><b>Why is MySQL a hard dependency while Redis may fail?</b></summary>
<br>
Different roles: Redis is a hot cache — when it dies we degrade to MySQL and just get slower. MySQL is the system of record — if it's gone, no context can land anywhere, so fail fast instead of running degraded.
<br><br><code>storage/mysql_store.py → self_check</code>
</details>

<details>
<summary><b>Why must the LangGraph checkpointer be AsyncSqliteSaver?</b></summary>
<br>
A real bug we hit: every API goes through <code>ainvoke</code>/<code>astream</code>, and the sync SqliteSaver raises <code>NotImplementedError</code> on async methods — the graph compiles fine, then the first chat crashes. Lesson: <b>a graph that compiles is not a graph that runs</b>. Test at the graph level (see <code>test_compliance_loop.py</code>).
</details>

<details>
<summary><b>Why aren't orders persisted in this system?</b></summary>
<br>
The CS system is a pass-through: query via MCP, put the result in the answer, drop it. The order system is the single source of truth; caching orders here only creates consistency problems.
</details>

<details>
<summary><b>Why SSE instead of WebSocket for streaming?</b></summary>
<br>
Chat streaming is mostly one-directional (server → client). SSE rides plain HTTP and reconnects automatically, and <code>graph.astream(stream_mode="updates")</code> is already an event generator — a perfect match.
</details>

<details>
<summary><b>Why "rules first, LLM second" for compliance?</b></summary>
<br>
Local regex checks catch common PII patterns and fixed forbidden terms before an LLM call. They can falsely match ordinary numbers and miss other expressions. The LLM reviews responses that pass those rules.
<br><br><code>agents/compliance_checker.py → full_check</code>
</details>

## ✅ Testing

```bash
pip install -r requirements-dev.txt
DASHSCOPE_API_KEY=dummy-key-for-ci python -m pytest -q
```

Local recheck on 2026-10-26 (including storage-fallback regression): 33 passed / 1 skipped — the skip is the roundtrip case without a real MySQL; in CI (Python 3.12 + real MySQL service) it executes for real.

| Module | Cases | What it verifies | Result |
|---|---|---|---|
| Compliance repair loop | 3 | Graph-level tests (FakeLLM driving the full LangGraph): pass on first try, degrade after 2 rewrites, pass after one rewrite | ✅ |
| Ticket idempotency & rate limit | 7 | Semantic key hit/isolation, arg whitelist for LLM extras, working-memory degradation, login 429, AsyncSqliteSaver lifecycle | ✅ |
| MCP resilience | 3 | Local tools & MCP share the idempotency store, MCP ticket idempotency | ✅ |
| MySQL fallback & degradation | 4 | Fail-fast self-check, in-process degradation; roundtrip when MySQL is reachable | ✅ / conditional |
| Working memory | 4 | Context read/write, history truncation, session clear, session isolation | ✅ |
| Fallback regression | 8 | Write-through fan-out (Redis+MySQL), data readable after Redis failure, runtime SQL errors fall back, data visible after recovery, Redis fail-back, JWT secret required, degraded retrieval uses same threshold | ✅ |
| Embedding provider | 5 | Dual-path selection (DashScope / OpenAI-compatible), auto index rebuild on identity mismatch, legacy metadata compat | ✅ |

> [!NOTE]
> `test_mysql_store_roundtrip` now uses an explicit `pytest.skip` when MySQL is unreachable (previously a silent `return` was counted as passed — fixed). CI starts a real MySQL service as a gate; the case must actually execute there.

<details>
<summary><b>End-to-end evaluation</b></summary>

The 8 scripts in `evaluation/` run against a live service: routing accuracy, RAG quality,
compliance interception, MCP resilience, memory consistency, auth security, plus locust
load tests. See each script's header for usage.
</details>

## 🛠️ Tech stack

| Layer | Tech |
|---|---|
| Orchestration | LangGraph · LangChain |
| Service | FastAPI · Uvicorn · SSE |
| Tool protocol | MCP (Model Context Protocol) |
| Storage | MySQL 8 · Redis · SQLite · FAISS |
| Frontend | React 19 · Vite · native SSE |
| Observability | OpenTelemetry · custom agent metrics |
| Quality | Pytest · GitHub Actions · Locust |
| Deployment | Docker Compose · Nginx |

## 📄 License

[MIT](LICENSE) © 2026

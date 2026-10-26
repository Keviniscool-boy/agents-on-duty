import json
import os
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi import Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from agents.supervisor import (
    close_checkpoint,
    create_sqlite_checkpointer,
    create_supervisor_graph,
)
from auth.database import (
    create_user,
    get_user_by_username,
    init_database,
    list_user_sessions,
    save_session_owner,
    session_exists,
    user_owns_session,
    verify_password,
)
from auth.security import assert_jwt_configured, create_access_token, get_current_user
from mcp_client.client import MCPClient
from mcp_tools.order_server import server as mcp_server
from memory.long_term import LongTermMemory
from memory.short_term import ShortTermMemory
from memory.working_memory import WorkingMemory
from storage.mysql_store import mysql_store
from tracing.otel_config import get_metrics, init_tracer, trace_agent_call


PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

working_memory = WorkingMemory()
short_term_memory = ShortTermMemory()
long_term_memory = LongTermMemory()
mcp_client = MCPClient(str(PROJECT_ROOT / "mcp_tools" / "order_server.py"))
graph: Any = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize shared services when the API starts and release them on shutdown."""
    global graph

    # 安全自检（B1）：JWT 密钥缺失直接拒绝启动，禁止使用默认密钥。
    assert_jwt_configured()

    # MySQL 是兜底存储，启动自检失败则拒绝启动。
    mysql_store.self_check()

    init_database()

    init_tracer(
        otlp_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
    )

    llm = ChatOpenAI(
        model=os.getenv("MODEL_NAME"),
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        base_url=os.getenv("OPENAI_BASE_URL"),
        temperature=0,
    )
    graph = create_supervisor_graph(
        llm,
        long_term_memory,
        working_memory,
        mcp_client,
        checkpointer=await create_sqlite_checkpointer(),
    )

    await mcp_client.connect()

    count = await long_term_memory.load_knowledge_base(
        str(PROJECT_ROOT / "knowledge_base")
    )
    if count:
        long_term_memory.save()
        print(f"Loaded and saved {count} knowledge chunks")
    else:
        print(
            f"Loaded {len(long_term_memory.documents)} persisted knowledge chunks"
        )

    yield

    await mcp_client.close()
    await close_checkpoint()
    graph = None


app = FastAPI(
    title="Smart Customer Service Agent",
    version="1.0.0",
    lifespan=lifespan,
)

# B5：CORS 白名单化。默认只放行本地前端；部署时用
# CORS_ALLOW_ORIGINS="https://app.example.com,https://admin.example.com" 覆盖。
_CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ALLOW_ORIGINS",
        "http://localhost:8080,http://localhost:5173",
    ).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ToolCallRequest(BaseModel):
    name: str
    arguments: dict = {}


class AuthRequest(BaseModel):
    username: str
    password: str


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict


class ChatRequest(BaseModel):
    message: str
    user_id: str = "anonymous"
    session_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    intent: str
    current_agent: str
    compliance_passed: bool
    compliance_reason: str


# 登录限流：同一 IP + 用户名在窗口期内最多尝试 LOGIN_MAX_ATTEMPTS 次。
# 计数优先存 Redis（跨 worker 共享，B6）；Redis 不可用时降级为
# 进程内滑动窗口（单 worker 有效，多 worker 防护减弱，见 README）。
LOGIN_WINDOW_SECONDS = 60
LOGIN_MAX_ATTEMPTS = 5
_login_attempts: dict[str, deque[float]] = defaultdict(deque)
_rate_limit_redis = None
_rate_limit_redis_failed = False


def _get_rate_limit_redis():
    """懒连接限流用的 Redis；失败后本进程不再重试（与主链路独立）。"""
    global _rate_limit_redis, _rate_limit_redis_failed
    if _rate_limit_redis is not None or _rate_limit_redis_failed:
        return _rate_limit_redis
    url = os.getenv("REDIS_URL")
    if not url:
        _rate_limit_redis_failed = True
        return None
    try:
        import redis as redis_sync

        client = redis_sync.Redis.from_url(url, decode_responses=True)
        client.ping()
        _rate_limit_redis = client
    except Exception:
        _rate_limit_redis_failed = True
    return _rate_limit_redis


def _check_login_rate_limit(client_ip: str, username: str) -> None:
    """超过限流阈值时抛 429，防止暴力试密码。"""
    redis_client = _get_rate_limit_redis()
    if redis_client is not None:
        redis_key = f"smartcs:login_rl:{client_ip}:{username}"
        try:
            count = redis_client.incr(redis_key)
            if count == 1:
                redis_client.expire(redis_key, LOGIN_WINDOW_SECONDS)
            if count > LOGIN_MAX_ATTEMPTS:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many login attempts, please retry later",
                )
            return
        except HTTPException:
            raise
        except Exception:
            pass  # Redis 故障 → 落到进程内兜底

    key = f"{client_ip}:{username}"
    now = time.monotonic()
    attempts = _login_attempts[key]
    while attempts and now - attempts[0] > LOGIN_WINDOW_SECONDS:
        attempts.popleft()
    if len(attempts) >= LOGIN_MAX_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts, please retry later",
        )
    attempts.append(now)


@app.get("/api/tools")
async def list_tools(current_user: dict = Depends(get_current_user)):
    """返回 MCP 工具列表，仅允许已登录用户查看。"""
    tools = await mcp_server.list_tools()
    tool_list = tools.tools if hasattr(tools, "tools") else tools
    return {"tools": [tool.model_dump() for tool in tool_list]}


@app.post("/api/tools/call")
async def call_tool(
    request: ToolCallRequest,
    current_user: dict = Depends(get_current_user),
):
    """调用 MCP 工具，仅允许已登录用户调用。"""
    result = await mcp_server.call_tool(
        name=request.name,
        arguments=request.arguments,
    )
    return result.model_dump()


async def _prepare_chat(
    request: ChatRequest,
    current_user: dict,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """校验会话归属、组装初始状态，chat 与 chat/stream 共用。"""
    if graph is None:
        raise HTTPException(status_code=503, detail="System is initializing")

    session_id = request.session_id or str(uuid.uuid4())
    if (
        request.session_id
        and session_exists(session_id)
        and not user_owns_session(session_id, current_user["id"])
    ):
        raise HTTPException(status_code=403, detail="Session does not belong to user")
    save_session_owner(session_id, current_user["id"])
    history = await short_term_memory.get_history(session_id)
    history_messages = []

    for message in history:
        if message["role"] == "user":
            history_messages.append(HumanMessage(content=message["content"]))
        elif message["role"] == "assistant":
            history_messages.append(AIMessage(content=message["content"]))

    initial_state = {
        "messages": [*history_messages, HumanMessage(content=request.message)],
        "user_id": current_user["id"],
        "session_id": session_id,
        "intent": "",
        "current_agent": "",
        "final_response": "",
        "compliance_passed": False,
        "compliance_reason": "",
        "compliance_retries": 0,
        "sub_results": {},
    }
    config = {"configurable": {"thread_id": session_id}}
    return session_id, initial_state, config


async def _finalize_chat(
    session_id: str,
    user_id: str,
    user_message: str,
    result: dict[str, Any],
) -> ChatResponse:
    """把图执行结果写入记忆并组装响应，chat 与 chat/stream 共用。"""
    response = result["final_response"]
    if not result["compliance_passed"]:
        response = "抱歉，该回答未通过合规审查，暂时无法提供详细信息。"

    working_memory.update(
        session_id,
        {
            "user_id": user_id,
            "last_intent": result.get("intent", ""),
            "current_agent": result.get("current_agent", ""),
            "compliance_passed": result.get("compliance_passed", False),
            "compliance_reason": result.get("compliance_reason", ""),
            "last_response": response,
        },
        user_id=user_id,
    )

    await short_term_memory.add_message(
        session_id, "user", user_message, user_id=user_id
    )
    await short_term_memory.add_message(
        session_id, "assistant", response, user_id=user_id
    )

    return ChatResponse(
        response=response,
        session_id=session_id,
        intent=result["intent"],
        current_agent=result["current_agent"],
        compliance_passed=result["compliance_passed"],
        compliance_reason=result["compliance_reason"],
    )


@app.post("/api/chat", response_model=ChatResponse)
@trace_agent_call("api")
async def chat(
    request: ChatRequest,
    current_user: dict = Depends(get_current_user),
):
    session_id, initial_state, config = await _prepare_chat(request, current_user)

    try:
        result = await graph.ainvoke(initial_state, config=config)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Agent processing failed",
        ) from exc

    return await _finalize_chat(
        session_id,
        current_user["id"],
        request.message,
        result,
    )


# 节点名 → 用户可读的处理阶段描述，SSE status 事件使用。
NODE_STATUS = {
    "supervisor_route": "正在判断问题类型",
    "knowledge_rag": "正在查询知识库",
    "ticket_handler": "正在处理业务请求",
    "fallback": "正在生成兜底回答",
    "compliance_checker": "正在进行合规检查",
    "rewrite": "正在根据合规反馈重写回答",
    "synthesize": "正在整理最终回答",
}


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.post("/api/chat/stream")
@trace_agent_call("api_stream")
async def chat_stream(
    request: ChatRequest,
    current_user: dict = Depends(get_current_user),
):
    """SSE 流式聊天：实时推送 Agent 处理阶段，最后给 final 事件。"""
    session_id, initial_state, config = await _prepare_chat(request, current_user)

    async def event_stream():
        yield _sse({"event": "session", "session_id": session_id})

        merged: dict[str, Any] = {}
        try:
            async for update in graph.astream(
                initial_state,
                config=config,
                stream_mode="updates",
            ):
                for node_name, delta in update.items():
                    if delta:
                        merged.update(delta)
                    yield _sse(
                        {
                            "event": "status",
                            "node": node_name,
                            "message": NODE_STATUS.get(node_name, node_name),
                        }
                    )
        except Exception:
            yield _sse({"event": "error", "message": "Agent processing failed"})
            return

        if not merged.get("final_response") and "fallback" not in merged:
            yield _sse({"event": "error", "message": "Agent processing failed"})
            return

        result = {
            "final_response": merged.get("final_response", ""),
            "intent": merged.get("intent", ""),
            "current_agent": merged.get("current_agent", ""),
            "compliance_passed": merged.get("compliance_passed", False),
            "compliance_reason": merged.get("compliance_reason", ""),
        }
        chat_response = await _finalize_chat(
            session_id,
            current_user["id"],
            request.message,
            result,
        )
        yield _sse({"event": "final", **chat_response.model_dump()})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/history/{session_id}")
async def get_history(
    session_id: str,
    current_user: dict = Depends(get_current_user),
):
    if not user_owns_session(session_id, current_user["id"]):
        raise HTTPException(status_code=403, detail="Session does not belong to user")
    history = await short_term_memory.get_history(session_id)
    return {"session_id": session_id, "messages": history}


@app.get("/health")
async def health_check():
    return {"status": "healthy", "version": "1.0.0"}


@app.get("/api/sessions")
async def get_sessions(current_user: dict = Depends(get_current_user)):
    """返回当前用户自己的会话列表。"""
    sessions = list_user_sessions(current_user["id"])
    result = []
    for session in sessions:
        history = await short_term_memory.get_history(
            session["session_id"],
            last_n=1,
        )
        first_message = history[0]["content"] if history else "新会话"
        result.append({**session, "title": first_message[:28]})
    return {"sessions": result}


@app.get("/api/metrics")
async def get_agent_metrics(current_user: dict = Depends(get_current_user)):
    return {"agent_metrics": get_metrics().get_summary()}


@app.post("/api/auth/register", response_model=AuthResponse, status_code=201)
async def register(request: AuthRequest):
    username = request.username.strip()
    if len(username) < 3 or len(request.password) < 6:
        raise HTTPException(
            status_code=400,
            detail="Username must have 3+ characters and password 6+ characters",
        )
    if get_user_by_username(username) is not None:
        raise HTTPException(status_code=409, detail="Username already exists")

    try:
        user = create_user(username, request.password)
    except Exception as exc:
        raise HTTPException(status_code=409, detail="Username already exists") from exc

    token = create_access_token(user["id"], user["username"])
    return AuthResponse(access_token=token, user=user)


@app.post("/api/auth/login", response_model=AuthResponse)
async def login(request: AuthRequest, http_request: Request):
    _check_login_rate_limit(http_request.client.host, request.username.strip())
    row = get_user_by_username(request.username.strip())
    if row is None or not verify_password(request.password, row["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    user = {
        "id": row["id"],
        "username": row["username"],
        "created_at": row["created_at"],
    }
    token = create_access_token(user["id"], user["username"])
    return AuthResponse(access_token=token, user=user)


@app.get("/api/auth/me")
async def me(current_user: dict = Depends(get_current_user)):
    return {"user": current_user}

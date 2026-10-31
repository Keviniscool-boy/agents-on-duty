import json
import os
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi import Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
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
from auth.security import (
    assert_jwt_configured,
    clear_auth_cookie,
    create_access_token,
    get_current_user,
    require_role,
    set_auth_cookie,
)
from auth.database import set_user_role
from mcp_client.client import MCPClient
from mcp_tools.order_server import server as mcp_server
from memory.long_term import LongTermMemory
from memory.short_term import ShortTermMemory
from memory.working_memory import WorkingMemory
from langgraph.types import Command
from storage.mysql_store import mysql_store
from storage.after_sales import (
    apply_refund,
    cancel_refund,
    get_order,
    get_refund_review,
    list_orders,
    list_refund_actions,
    list_refund_reviews,
    list_refunds,
    record_refund_decision,
)
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
    elif long_term_memory.index_build_failed:
        print("Knowledge index update failed; previous index retained")
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
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_CORS_ORIGINS_SET = {origin.rstrip("/") for origin in _CORS_ORIGINS}


@app.middleware("http")
async def csrf_origin_check(request, call_next):
    """B10 CSRF 纵深防御：SameSite=Strict 之外校验 Origin。

    浏览器跨站请求会带 Origin；非浏览器客户端（curl/评测脚本）
    不带 Origin，直接放行，由 JWT 鉴权兜底。
    """
    if request.method in {"POST", "PUT", "DELETE", "PATCH"}:
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") not in _CORS_ORIGINS_SET:
            return JSONResponse(
                status_code=403,
                content={"detail": "Origin not allowed"},
            )
    return await call_next(request)


class ToolCallRequest(BaseModel):
    name: str
    arguments: dict = {}


class RefundRequest(BaseModel):
    order_id: str
    reason: str


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


class DecisionRequest(BaseModel):
    action: str
    note: str = ""


class RoleGrantRequest(BaseModel):
    username: str
    role: str


class ChatResponse(BaseModel):
    response: str
    session_id: str
    intent: str
    current_agent: str
    compliance_passed: bool
    compliance_reason: str
    # 阶段二：本轮触发了人工审核暂停（interrupt），等待客服决定。
    awaiting_human: bool = False


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

        client = redis_sync.Redis.from_url(
            url,
            decode_responses=True,
            # 审核追加：不设超时的话，Redis 指向不可达远端时首次登录
            # 会阻塞到 TCP 超时（数十秒级），拖垮登录接口。
            socket_connect_timeout=2,
            socket_timeout=2,
        )
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
    if request.name not in {"order_query", "order_list", "refund_apply", "ticket_create", "knowledge_search", "risk_check"}:
        raise HTTPException(status_code=400, detail="Unsupported tool")
    arguments = dict(request.arguments)
    if request.name in {"order_query", "order_list", "refund_apply", "risk_check"}:
        arguments["user_id"] = current_user["id"]
    if request.name == "ticket_create":
        from agents.ticket_handler import TicketHandlerAgent

        arguments["idempotency_key"] = TicketHandlerAgent.build_idempotency_key(
            current_user["id"], arguments
        )
    result = await mcp_server.call_tool(
        name=request.name,
        arguments=arguments,
    )
    return result.model_dump()


@app.get("/api/orders")
def orders(current_user: dict = Depends(get_current_user)):
    return {"orders": list_orders(current_user["id"])}


@app.get("/api/orders/{order_id}")
def order_detail(order_id: str, current_user: dict = Depends(get_current_user)):
    result = get_order(current_user["id"], order_id)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result)
    return result


@app.get("/api/refunds")
def refunds(current_user: dict = Depends(get_current_user)):
    return {"refunds": list_refunds(current_user["id"])}


@app.post("/api/refunds")
def create_refund(request: RefundRequest, idempotency_key: str = Header(default="", alias="Idempotency-Key"), current_user: dict = Depends(get_current_user)):
    result = apply_refund(current_user["id"], request.order_id, request.reason, idempotency_key)
    if "error" in result:
        code = 404 if result["error"] == "order_not_found" else 409 if result["error"] in {"refund_exists", "idempotency_conflict"} else 400
        raise HTTPException(status_code=code, detail=result)
    return result


@app.post("/api/refunds/{refund_id}/cancel")
def cancel_refund_request(refund_id: str, current_user: dict = Depends(get_current_user)):
    result = cancel_refund(current_user["id"], refund_id)
    if "error" in result:
        code = 404 if result["error"] == "refund_not_found" else 409
        raise HTTPException(status_code=code, detail=result)
    return result


async def _resume_handoff(session_id: str, decision: dict, owner_user_id: str) -> dict:
    """阶段二：恢复被 human_review 暂停的线程；未暂停时为 no-op。

    幂等保护双保险：决定写入前的状态机守卫挡住重复审批，
    这里的 interrupts 检查挡住对非暂停线程的误恢复。
    恢复产生的最终答复写入客户会话历史，客户侧立即可见。
    """
    config = {"configurable": {"thread_id": session_id}}
    if graph is None:
        # 图未初始化（测试或降级场景）：决定已落库，无线程可恢复。
        return {"resumed": False, "response": ""}
    state = await graph.aget_state(config)
    if not any(task.interrupts for task in state.tasks):
        return {"resumed": False, "response": ""}
    result = await graph.ainvoke(Command(resume=decision), config=config)
    response = result.get("final_response") or "人工审核已完成。"
    await short_term_memory.add_message(
        session_id, "assistant", response, user_id=owner_user_id
    )
    return {"resumed": True, "response": response}


@app.get("/api/workbench/refunds")
def workbench_refunds(operator: dict = Depends(require_role("agent", "admin"))):
    """客服工作台：退款申请列表（仅 agent/admin 可见）。"""
    return {"refunds": list_refund_reviews()}


@app.get("/api/workbench/refunds/{refund_id}")
async def workbench_refund_detail(
    refund_id: str,
    operator: dict = Depends(require_role("agent", "admin")),
):
    """工作台详情：申请 + 订单 + 审计 + 暂停线程的交接单 + 会话历史。

    客服可以查看处于售后流程中的会话内容，这是显式授权的越权；
    普通用户访问工作台一律 403，既有 user_owns_session 语义不受影响。
    """
    review = get_refund_review(refund_id)
    if review is None:
        raise HTTPException(status_code=404, detail={"error": "refund_not_found", "message": "退款申请不存在"})
    session_id = review.get("session_id") or ""
    thread_paused = False
    handoff_payload = None
    if session_id and graph is not None:
        config = {"configurable": {"thread_id": session_id}}
        state = await graph.aget_state(config)
        for task in state.tasks:
            if task.interrupts:
                thread_paused = True
                handoff_payload = task.interrupts[0].value
                break
    history = await short_term_memory.get_history(session_id) if session_id else []
    return {
        "refund": review,
        "session_id": session_id,
        "thread_paused": thread_paused,
        "handoff": handoff_payload,
        "history": history,
        "actions": list_refund_actions(refund_id),
    }


@app.post("/api/workbench/refunds/{refund_id}/decision")
async def decide_refund(
    refund_id: str,
    request: DecisionRequest,
    operator: dict = Depends(require_role("agent", "admin")),
):
    """人工决定：批准 / 拒绝 / 需补充信息；有暂停线程时恢复图执行。"""
    if request.action not in {"approve", "reject", "need_info"}:
        raise HTTPException(status_code=400, detail={"error": "invalid_action", "message": "不支持的操作类型"})
    if request.action == "need_info" and not request.note.strip():
        raise HTTPException(status_code=400, detail={"error": "note_required", "message": "要求补充信息时必须填写追问内容"})

    review = get_refund_review(refund_id)
    if review is None:
        raise HTTPException(status_code=404, detail={"error": "refund_not_found", "message": "退款申请不存在"})

    result = record_refund_decision(refund_id, request.action, request.note, operator["id"])
    if "error" in result:
        code = 404 if result["error"] == "refund_not_found" else 409 if result["error"] == "invalid_state" else 400
        raise HTTPException(status_code=code, detail=result)

    decision = {
        "action": request.action,
        "note": request.note.strip()[:500],
        "operator": operator["username"],
    }
    resumed = {"resumed": False, "response": ""}
    session_id = review.get("session_id") or ""
    if session_id:
        resumed = await _resume_handoff(session_id, decision, review["user_id"])

    return {
        "refund": result,
        "resumed": resumed["resumed"],
        "agent_response": resumed["response"],
        "actions": list_refund_actions(refund_id),
    }


@app.post("/api/admin/users/role")
def grant_user_role(
    request: RoleGrantRequest,
    admin: dict = Depends(require_role("admin")),
):
    """管理员授予/变更角色。角色白名单：user / agent / admin。"""
    if request.role not in {"user", "agent", "admin"}:
        raise HTTPException(status_code=400, detail={"error": "invalid_role", "message": "角色只能是 user、agent 或 admin"})
    row = set_user_role(request.username, request.role)
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "user_not_found", "message": "用户不存在"})
    return {"user": dict(row)}


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
        # 阶段二：显式清空跨轮残留的暂停信号，防止上一轮的
        # pending_handoff 让本轮误入人工审核节点。
        "pending_handoff": {},
        "handoff_decision": {},
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
    interrupted = bool(result.get("__interrupt__"))
    response = result["final_response"]
    if interrupted:
        # 图暂停在 human_review：本轮给用户的是等待提示，
        # 最终答复由客服决定后的恢复流程写入会话历史。
        response = "您的退款申请已提交人工审核，客服处理后将继续为您跟进。"
    elif not result["compliance_passed"]:
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
        awaiting_human=interrupted,
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
    "human_review": "已转人工审核，等待客服处理",
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
        interrupted = False
        try:
            async for update in graph.astream(
                initial_state,
                config=config,
                stream_mode="updates",
            ):
                for node_name, delta in update.items():
                    if node_name == "__interrupt__":
                        # 暂停信号：delta 是 Interrupt 元组，不能混进 merged。
                        interrupted = True
                        continue
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

        if (
            not merged.get("final_response")
            and "fallback" not in merged
            and not interrupted
        ):
            yield _sse({"event": "error", "message": "Agent processing failed"})
            return

        result = {
            "final_response": merged.get("final_response", ""),
            "intent": merged.get("intent", ""),
            "current_agent": merged.get("current_agent", ""),
            "compliance_passed": merged.get("compliance_passed", False),
            "compliance_reason": merged.get("compliance_reason", ""),
            "__interrupt__": interrupted,
        }
        if result["__interrupt__"]:
            yield _sse(
                {
                    "event": "status",
                    "node": "human_review",
                    "message": "已转人工审核，等待客服处理",
                }
            )
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
    knowledge_status = long_term_memory.knowledge_status()
    overall_status = {
        "degraded": "degraded",
        "empty": "degraded",
        "unknown": "unknown",
        "ready": "healthy",
    }[knowledge_status]
    return {
        "status": overall_status,
        "version": "1.0.0",
        "knowledge_base": {"status": knowledge_status, "documents": len(long_term_memory.documents)},
    }


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
async def register(request: AuthRequest, response: Response):
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
    set_auth_cookie(response, token)
    return AuthResponse(access_token=token, user=user)


@app.post("/api/auth/logout")
async def logout(response: Response):
    """B10：登出即清除 httpOnly Cookie。"""
    clear_auth_cookie(response)
    return {"detail": "logged out"}


@app.post("/api/auth/login", response_model=AuthResponse)
async def login(request: AuthRequest, http_request: Request, response: Response):
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
    set_auth_cookie(response, token)
    return AuthResponse(access_token=token, user=user)


@app.get("/api/auth/me")
async def me(current_user: dict = Depends(get_current_user)):
    return {"user": current_user}

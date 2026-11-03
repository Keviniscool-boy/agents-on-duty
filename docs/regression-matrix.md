# 故障与回归矩阵（阶段五）

阶段五的回归范围：**可观测性 + 故障恢复 + 回归评测**。本矩阵把路线图要求的场景映射到具体用例，避免“跑过测试”却无法解释失败原因。

## 故障场景

| 场景 | 覆盖方式 | 用例 |
|---|---|---|
| MCP 超时 | 单元测试（重试后降级本地工具） | `test_stage5.py::test_mcp_timeout_falls_back_to_local_tool` |
| MCP 幂等/降级 | 单元测试 | `test_mcp_resilience.py::test_local_ticket_create_is_idempotent`、`test_mcp_and_local_tools_share_idempotency_store` |
| LLM 非法 JSON | 单元测试（合规检查拒答） | `test_stage5.py::test_compliance_invalid_json_is_rejected` |
| Redis 不可用 | 单元测试（进程内兜底） | `test_idempotency_and_limits.py::test_working_memory_fallback_without_redis`、`test_mysql_fallback.py::test_working_memory_degrades_to_in_process_when_all_backends_down` |
| 审批后重启 | 单元测试（持久化 checkpoint 恢复） | `test_human_in_the_loop.py::test_restart_recovery_resumes_from_persistent_checkpoint`、`test_pending_outbox_survives_restart_and_resumes_once` |
| 恢复失败重试 | 单元测试 | `test_human_in_the_loop.py::test_resume_failure_is_retryable_and_notification_is_not_duplicated`、`test_checkpoint_completed_before_notice_can_be_retried` |
| 重复提交 | 单元测试（幂等键） | `test_policy_mvp.py::test_quality_after_seven_days_and_idempotent_replay`、`test_idempotency_and_limits.py::test_ticket_semantic_idempotency` |

## 回归场景

| 场景 | 覆盖方式 | 用例/套件 |
|---|---|---|
| 正常问答/退款 | pytest + RAG 评测 | `test_after_sales.py`、`test_policy_mvp.py`、`evaluation/run_rag_eval.py` |
| 越权访问 | pytest | `test_admin_audit.py::test_admin_routes_require_admin`、`test_human_in_the_loop.py::test_foreign_legacy_session_cannot_be_read_or_approved` |
| 拒答 | pytest + RAG 评测 | `test_stage4.py::test_knowledge_rag_refuses_without_results`、`rag_cases.jsonl` 的 `expected_action=refuse` |
| 转人工 | pytest + RAG 评测 | `test_stage4.py::test_knowledge_rag_refuses_without_results`（`handoff_required`）、`rag_cases.jsonl` 的 `expected_action=handoff` |
| 恢复 | pytest | 见上表“审批后重启/恢复失败重试” |
| 幂等 | pytest | 见上表“重复提交” |

## 可观测性

| 内容 | 实现 |
|---|---|
| 请求级 trace ID | `tracing/request_context.py`：每个 HTTP 请求生成/透传 `X-Trace-Id`，写入 `ChatResponse.trace_id` |
| 节点耗时 | `trace_agent_call` 写入 `admin_events.duration_ms` |
| 模型调用次数/耗时/错误 | `tracing/llm.py` 的 `TracedChatModel` 写入 `admin_events(kind=model)` 与 `AgentMetrics.model_calls` |
| 工具调用/失败/降级 | `ticket_handler` 写入 `admin_events(kind=tool)`，含 `retries/error` |
| 错误与降级原因 | 节点异常类名、工具 `error_type`、存储 `storage_degradation` 计数 |

## 版本化评测

- 套件版本：`evaluation/suite.py::SUITE_VERSION`（当前 `2026.11-stage5`）。
- 清单：`evaluation/manifest.json`（套件、case 文件、场景、回归矩阵路径）。
- 报告：`evaluation/run_stage5_suite.py` 写出 `evaluation/results/stage5-latest.json`，包含运行环境、模型/embedding 配置、样本规模、逐条结果与失败案例。
- 前后对照：`evaluation/compare.py` 仅在 `suite_version` + `model`/`embedding_model`/`embedding_provider` 一致时输出 delta。

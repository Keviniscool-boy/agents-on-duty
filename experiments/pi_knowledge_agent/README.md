# Pi Knowledge Agent Experiment (Stage 6)

独立实验目录：**Pi 只负责知识库问答**，通过稳定 Python 知识服务检索、组合多个政策来源并生成带引用的答案。退款、审批、中断/恢复、权限、幂等和审计仍由 LangGraph + FastAPI 控制。

## 为什么用 Pi 做知识问答

- Pi (`@earendil-works/pi-agent-core`) 是通用 agent loop：模型自己决定何时调用 `knowledge_search`、换关键词再查、把多个来源拼起来。
- 知识问答没有状态机，失败可以重试或拒答，适合交给通用工具调用循环。
- 与业务代码解耦：Pi 进程只依赖 HTTP API / MCP，不需要接触 SQLite、理赔状态机或权限。

## 为什么不把业务流程交给 Pi

- 退款/审批需要确定性的状态迁移、`interrupt`/`Command(resume)`、幂等键、角色权限和审计。
- 这些约束已经在 LangGraph 节点、FastAPI 依赖和事务里实现；换成通用 agent 会失去可验证的边界。
- 因此 Pi 只作为 `knowledge_rag` 的可对比替代，不进入 `/api/refunds`、`/api/workbench/*`、`/api/admin/*`。

## 运行

```bash
# 1. 启动 Python API（Docker 或 uvicorn）
docker compose up --build -d

# 2. 获取 token（登录接口会 Set-Cookie，也可从响应 access_token 取）
#    这里用环境变量传入 Bearer token
export KNOWLEDGE_API_URL=http://127.0.0.1:8000
export KNOWLEDGE_API_TOKEN=<登录返回的 access_token>

# 3. 构建并运行 Pi 实验
cd experiments/pi_knowledge_agent
npm install
npm run build
MODEL_NAME=gpt-4o-mini \
OPENAI_API_KEY=... \
OPENAI_BASE_URL=... \
node dist/index.js --question "商品签收后多少天内可以申请退货？" --json
```

输出 JSON：`answer`、`sources`、`hits`、`latency_ms`、`input_tokens`、`output_tokens`、`cost_usd`、`error`。

## MCP 变体

`python -m mcp_tools.knowledge_server` 通过 stdio 暴露 `knowledge_search` 与 `knowledge_status`。
Pi 侧可用 `@earendil-works/pi-mcp` 的 `McpClient`/`StdioTransport` 包装成 `AgentTool`（见 pi-mcp README 的 “Wrapping an MCP tool” 示例），从而完全不依赖 HTTP token。

## 对比

```bash
python evaluation/pi_comparison.py --limit 10
```

- 基线：LangGraph `KnowledgeRAGAgent`（向量+关键词混合检索 + LLM 生成）。
- 实验：Pi CLI（同一套 `rag_cases.jsonl`，同一 Python 知识服务）。
- 指标：回答关键词命中、引用正确率、拒答准确率、中位延迟、模型成本、失败率。
- 决策：只有引用正确率提升 ≥5 个百分点、失败率不升高、中位延迟不超过基线 1.5 倍、成本在预算内，才建议扩大 Pi。
- 报告写入 `evaluation/results/pi-comparison-latest.json`。

## 限制

- 政策与订单仍为模拟数据。
- Pi 实验需要真实模型与 embedding 服务；本仓库的 pytest 只覆盖知识服务、MCP adapter、对比/决策逻辑与静态契约，不做真实模型 E2E。
- 当前结论：**暂不扩大 Pi 使用范围**，保留独立实验目录；真实数据由 `evaluation/pi_comparison.py` 产生后再评估。

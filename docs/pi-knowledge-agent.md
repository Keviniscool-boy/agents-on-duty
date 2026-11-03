# Pi 知识助手实验（阶段六）

## 结论先行

**Pi 适合知识库问答，不适合替换业务工作流。**

- Pi 作为知识问答子 Agent：负责选择检索工具、多次检索、组合多个政策来源、生成带引用的答案。
- LangGraph + FastAPI 继续负责退款、审批、`interrupt`/`resume`、权限、幂等、审计和订单状态机。
- 阶段六代码已合并（PR #13），但 Pi 仍为独立实验、暂不扩大使用范围。真实模型端到端对比尚未执行，因此这不能当作量化实验结论；需要先在部署环境运行 `evaluation/pi_comparison.py` 并配置可靠价格。

## 架构

```text
用户问题
   │
   ├── LangGraph（主流程，未改变）
   │      ├── supervisor → knowledge_rag / ticket_handler / fallback
   │      ├── human_review（interrupt/resume）
   │      └── 退款、权限、幂等、审计
   │
   └── Pi 实验（独立进程）
          └── Agent + knowledge_search 工具
                 ├── HTTP: POST /api/knowledge/search
                 └── MCP:  python -m mcp_tools.knowledge_server
                            └── KnowledgeService（同一套政策检索）
```

两条路径读取同一份 `knowledge_base/` 与 `KnowledgeService`，因此对比的是 **Agent 编排/引用方式**，不是数据差异。

## 为什么 Pi 适合知识 Agent

1. **工具选择循环**：知识问题经常需要换关键词重查、比较多个政策条款；Pi 的 agent loop 原生支持工具调用与多轮检索。
2. **无状态机负担**：知识问答失败可以拒答或转人工，不需要事务性状态迁移。
3. **可替换性**：Pi 只依赖稳定 API/MCP，不接触业务库；实验失败不影响主流程。

## 为什么业务工作流仍由 LangGraph 控制

1. **确定性状态**：退款申请 → 人工审核 → 批准/拒绝/补充信息 → 恢复执行，必须可验证、可重放。
2. **中断/恢复**：LangGraph `interrupt` + `Command(resume)` 与 SQLite checkpoint 已经覆盖重启恢复。
3. **权限与幂等**：角色门控、最后管理员保护、幂等键、同订单唯一申请，都在 FastAPI/事务层实现。
4. **审计**：节点/工具/模型事件、trace ID、管理员审计需要结构化落库；通用 agent 的自由文本循环不适合作为唯一审计来源。

## 对比方法

`evaluation/pi_comparison.py` 对同一批 `rag_cases.jsonl` 跑两条路径：

| 指标 | 说明 |
|---|---|
| 回答关键词命中 | 答案是否包含期望关键词 |
| 来源匹配率（初步） | 检索来源是否命中期望 `doc_id`，回答是否出现该来源 |
| 完整来源字符串匹配率（非断言级验证） | 答案是否出现完整来源串（`doc_id` + 版本 + 章节）；不证明结论被该来源支持 |
| 拒答准确率 | 负样本是否正确拒答 |
| 中位延迟 | 单题中位耗时（仅有效结果） |
| 模型成本 | Pi 报告的 `usage.cost.total` 汇总；未配置价格时记为**未知**，不按 0 计算 |
| 失败率 | 抛错/非零退出/JSON 解析失败；失败样本**不计入质量分母** |

扩展判定（`decide`）：**完整来源字符串匹配**提升 ≥5 个百分点、失败率不高于基线、中位延迟 ≤ 基线 1.5 倍、且成本已知并在预算内，才建议扩大 Pi；成本未知或有效样本不足时一律不自动扩大。判定口径记录为 `citation_full_string_match_only`，它不是断言级引用验证。

## 运行

见 [experiments/pi_knowledge_agent/README.md](../experiments/pi_knowledge_agent/README.md)。

```bash
python evaluation/pi_comparison.py --limit 10
```

报告：`evaluation/results/pi-comparison-latest.json`（gitignored），包含 `summary.rag`、`summary.pi`、`summary.comparison`、`summary.decision` 与逐题结果。

## 限制

- 政策/订单为模拟数据；未接真实企业政策、支付或物流。
- Pi 实验依赖真实模型与 embedding；pytest 覆盖知识服务、MCP adapter、对比逻辑与静态契约，不做真实模型 E2E。
- 来源匹配率只检查期望来源/完整来源串是否出现，**不能证明每条结论都被对应条款支持**，也不能充分识别编造引用；逐条断言级验证仍待补充。
- 两种实现的提示词、模型和重试策略不同，对比结果应结合成本与维护成本解读，不能只看单题质量。

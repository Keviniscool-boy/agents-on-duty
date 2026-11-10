import type { AgentTool } from "@earendil-works/pi-agent-core";

export interface KnowledgeHit {
  doc_id: string;
  source: string;
  content: string;
  score: number;
  retrieval: string;
  version: string;
  section: string;
}

export interface KnowledgeToolOptions {
  apiUrl: string;
  token: string;
  topK?: number;
  minScore?: number;
  onHits?: (hits: KnowledgeHit[]) => void;
}

const parameters = {
  type: "object",
  properties: {
    query: { type: "string", description: "用户问题或改写后的检索查询。" },
    top_k: { type: "number", description: "返回条数，默认 5。" },
  },
  required: ["query"],
};

/**
 * 把后端知识检索接口包装成实验智能体可用的检索工具。
 *
 * 政策检索的事实来源刻意收敛在后端服务一处；实验智能体只负责
 * 决定何时调用、如何引用检索结果，不自行实现检索。
 */
export function createKnowledgeSearchTool(
  options: KnowledgeToolOptions,
): AgentTool<any> {
  return {
    name: "knowledge_search",
    label: "知识库检索",
    description:
      "检索已发布的企业政策，返回来源、版本、章节与原文片段。回答政策问题前必须调用。",
    parameters,
    execute: async (_toolCallId: string, params: any) => {
      const response = await fetch(
        `${options.apiUrl.replace(/\/$/, "")}/api/knowledge/search`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${options.token}`,
          },
          body: JSON.stringify({
            query: params.query,
            top_k: params.top_k ?? options.topK ?? 5,
            min_score: options.minScore ?? 0.5,
          }),
        },
      );
      if (!response.ok) {
        const detail = await response.text();
        throw new Error(
          `knowledge_search failed: ${response.status} ${detail.slice(0, 200)}`,
        );
      }
      const data = (await response.json()) as { hits: KnowledgeHit[] };
      const hits = data.hits ?? [];
      options.onHits?.(hits);
      return {
        content: [
          { type: "text" as const, text: JSON.stringify(hits, null, 2) },
        ],
        details: { hits: hits.length },
      };
    },
  };
}

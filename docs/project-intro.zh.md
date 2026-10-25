# 📖 项目介绍与面试题库

> 本文是 [agents-on-duty](https://github.com/Keviniscool-boy/agents-on-duty) 的深度介绍，从面试官视角拆解这个项目的每一个设计决策。所有问题都在代码库中有真实对应，标注了文件位置。

---

## 一、项目背景：解决什么问题

做一个"能跑的 Agent demo"很容易，一个下午就行。但把一个多 Agent 系统做到**能讲清楚每个失效场景**，是另一回事。这个项目模拟的是金融/电商客服的真实约束：

- 回答必须**合规**——不能泄露隐私、不能承诺收益、不能暴露系统内部信息
- 业务操作必须**幂等**——用户重复提交工单不能建两张
- 依赖必须**会坏**——Redis 会挂、MCP 会超时、LLM 会返回非法 JSON
- 一切操作必须**可审计**——谁在哪个会话问了什么、走了哪个 Agent、合规是否通过

所以它不是一个"Agent 演示"，而是一个把这些约束全部落地的**完整工程**。

## 二、一条消息的完整旅程

以用户发送「查询订单 ORD-20260401-001 的物流」为例，全链路 8 步：

| 步骤 | 发生什么 | 代码位置 |
|---|---|---|
| 1 | 请求到达 `/api/chat/stream`，JWT 校验身份，会话归属校验 | `api/main.py` |
| 2 | 从短期记忆（Redis→MySQL 降级链）拉取对话历史，拼装初始 State | `api/main.py → _prepare_chat` |
| 3 | **Supervisor 路由**：先走确定性快路径（正则命中订单号 → 直接分流，0 次 LLM 调用）；未命中才走 LLM 路由，路由上下文来自工作记忆 | `agents/supervisor.py → route_decision` |
| 4 | **TicketHandler 接手**：拉取 MCP 工具列表转成 function schema，LLM 决定调用 `order_query`；网络异常按 1s/2s 退避重试，仍失败降级本地工具 | `agents/ticket_handler.py` |
| 5 | **合规检查**：规则先行（正则查 PII、敏感词，毫秒级、零成本），通过后才做 LLM 语义审查 | `agents/compliance_checker.py → full_check` |
| 6 | 若不合规 → **rewrite 节点**带违规原因重写 → 回到合规检查，conditional edge 控制最多 2 次，耗尽则合成节点输出降级话术 | `agents/supervisor.py → route_after_compliance` |
| 7 | **Synthesizer 合成**最终回答；全过程中 `graph.astream(stream_mode="updates")` 把每个节点完成事件实时推给前端 | `api/main.py → chat_stream` |
| 8 | 回答写入短期记忆、路由状态写入工作记忆（Redis→MySQL→进程内），LangGraph 断点存入 AsyncSqliteSaver | `api/main.py → _finalize_chat` |

## 三、架构分层与技术选型

| 层 | 选型 | 为什么 |
|---|---|---|
| 编排 | LangGraph StateGraph | 需要条件边和循环（合规重写回路）；checkpointer 免费获得断点恢复 |
| 服务 | FastAPI + SSE | 原生 async；SSE 单向推送够用，比 WebSocket 简单一档 |
| 工具协议 | MCP | 标准协议，工具可被任何 MCP 客户端复用，而不是私有 function calling |
| 热存储 | Redis | 对话历史 30min TTL、路由上下文 24h TTL，天然过期 |
| 兜底存储 | MySQL | 上下文审计需要落库；启动自检失败拒绝启动，保证"要么完整工作要么不工作" |
| 断点/幂等/账户 | SQLite | 三个轻量单机数据各用一个文件，不值得为此引入重型数据库 |
| 检索 | FAISS + DashScope Embeddings | 知识库仅 5 篇客服文档（约 30 行文本），本地向量索引足够，无需向量数据库服务 |
| 前端 | React + Vite + 原生 fetch 流 | 不引入 SSE 客户端库，`ReadableStream` 手写解析器不到 30 行 |

**代码规模**：后端核心约 2200 行、前端约 900 行、pytest 21 个用例、评测脚本 8 个。调参过程的真实评测数据见 [test-record.md](test-record.md)——路由准确率 66.67%→100%、RAG 阈值 0.6→0.55 后命中率 92%，这些"发现 bug→修复→复测"的迭代记录本身就是很好的面试素材。

## 四、面试官会怎么问

### 🧭 架构与编排

**Q1：为什么用 LangGraph，而不是手写 if/else 或者 crewAI？**
> 要点：核心需求是**条件边 + 环**（合规重写回路），if/else 写不出"回到之前节点"的循环语义；crewAI 是角色抽象、隐藏控制流，而客服系统需要**显式控制**每一步（审计要求）。LangGraph 的 StateGraph + conditional edge 正好是状态机模型；checkpointer 还白送断点恢复。诚实补充：如果不需要循环，手写顺序调用也够——选型是需求驱动的。

**Q2：路由为什么做"确定性快路径 + LLM"双轨？**
> 要点：高频问题（带订单号、问退货政策）用正则直接分流，省一次 LLM 调用、省延迟、结果 100% 稳定；LLM 只兜长尾。这是成本/延迟/准确率的三角权衡，也是渐进式上 LLM 的常见模式。代码：`_deterministic_intent`。

**Q3：合规检查为什么是独立节点，而不是嵌在业务 Agent 里？**
> 要点：①**关注点分离**——业务 Agent 的提示词已经很长，再塞合规规则会互相干扰；②**统一闸门**——所有分支（RAG/工单/兜底）共用一个出口，避免某个 Agent 忘写检查；③**可独立演进**——规则库、LLM 审查提示词可以单独迭代和评测（`evaluation/run_compliance_eval.py`）。

**Q4：重写回路为什么限制 2 次？**
> 要点：不设上限 = 同一个 LLM 可能无限次踩同一条规则，白烧 token 还拖延迟。2 次是"给一次改正机会"的工程化表达；耗尽后**降级为固定话术**而不是硬拦截报错，用户侧体验是平滑的。追问"为什么不是 1 次或 3 次"——1 次不给模型反馈消化的余地，3 次边际收益递减，这是拍板的可调参数（`MAX_COMPLIANCE_RETRIES`）。

### 🛡️ 合规与安全

**Q5：合规为什么"规则先行，LLM 补充"？**
> 要点：正则查 PII/敏感词是确定性的——零成本、毫秒级、可解释；LLM 只补语义层（变相收益承诺）。规则判高危直接短路，省 LLM 调用。两层各自会漏：正则漏语义、LLM 漏长尾格式，叠起来互补。

**Q6：手机号/银行卡的正则会误伤吗？**
> 要点：会。`bank_card` 是 16-19 位数字，会命中普通长数字（如时间戳拼接）。真实生产应该加 Luhn 校验、上下文关键词（"卡号"）加权。这个项目里误伤的代价只是"触发脱敏"，安全侧宁枉勿纵——但要能讲清楚这个取舍。

**Q7：JWT 没有吊销机制，怎么补？**
> 要点：当前登出只是前端删 token，签发的 JWT 到期前一直有效。补法：①短过期 + refresh token；②Redis/MySQL 黑名单，`get_current_user` 里多查一次；③或改用 opaque token + 服务端会话。能主动说出"现在有这个洞"比假装没有更加分。

**Q8：登录限流为什么用 IP+用户名做 key？**
> 要点：纯 IP 会误伤同一办公室的同事，纯用户名会让攻击者换 IP 绕过。双 key 折中。另外这是进程内 dict——多 worker 不共享、重启清零，生产要换 Redis，这个局限我是知道的（`api/main.py → _check_login_rate_limit`）。

**Q8b：JWT 的密钥管理有什么隐患？**
> 要点：`auth/security.py` 里 `JWT_SECRET_KEY` 未配置时会**静默退到硬编码的开发默认值**——生产忘配密钥就等于密钥公开。修法：①启动时校验该环境变量存在，缺失即拒绝启动（和 MySQL 自检同一哲学）；②或者至少打 warning 日志。能主动指出"我代码里有个静默降级的安全隐患"是加分项。

### 🎫 数据与幂等

**Q9：幂等键为什么用内容指纹而不是请求 ID？**
> 要点：请求 ID 每次调用都变，防得住重试防不住"用户再说一遍"。`user_id + sha256(标题+描述+分类)` 让语义重复天然去重。取舍：同内容只能提一次——客服场景这是 feature（"您已有工单 TK-xxx"），不是 bug。

**Q10：并发下两个请求同时建单怎么办？**
> 要点：`idempotency_key` 是 SQLite 主键，后到者 `IntegrityError` → 捕获 → 回查已有记录返回。不靠分布式锁，靠数据库唯一约束兜底，这是最朴素的正确答案。代码：`tools/ticket_store.py`。

**Q11：订单数据为什么不在系统里落库？**
> 要点：单一事实源原则。客服是透传层，缓存订单制造一致性问题；要缓存也应该在订单系统侧加。

**Q12：MCP 挂了怎么办？**
> 要点：三段式——①网络类异常（Timeout/ConnectionError）指数退避重试 2 次；②仍失败降级到本地同名工具（同一套业务逻辑）；③关键是**两级实现共享同一幂等存储**，重试→降级的整个链路里同一张工单只建一次。业务错误（如订单不存在）不重试——重试一个确定性失败没有意义。

### 🧠 记忆设计

**Q13：三层记忆分别存什么、为什么这么分？**
> 要点：短期记忆存"说过什么"（对话消息），工作记忆存"走到哪了"（路由状态/合规结果），长期记忆存"世界是什么样"（知识库向量）。三者生命周期不同：30 分钟 / 会话级 / 永久。和 LangGraph checkpointer 也不重复——checkpointer 是状态机快照用于恢复，working memory 是给提示词用的轻量上下文。

**Q14：为什么 MySQL 是硬依赖，Redis 却允许挂？**
> 要点：失效语义不同。Redis 是热缓存，挂了降级 MySQL 只是变慢；MySQL 是兜底数据源，它挂了意味着一切无处落地——快速失败比带病运行好。启动时 `self_check()` 先 ping 再建表，失败直接终止进程。

**Q15：Redis 和 MySQL 都挂了呢？**
> 要点：降级到进程内 dict，系统仍可用（单机模式下等于没有降级发生）。这条链的设计原则：**每往下一级，持久性变差，可用性不变**。面试官如果追问"多 worker 怎么办"——进程内各存各的会不一致，所以多 worker 部署时 Redis/MySQL 至少要有一个是硬要求。

### ⚙️ 工程与部署

**Q16：checkpointer 那个 AsyncSqliteSaver 的坑，具体是怎么回事？**
> 要点：所有 API 走 `ainvoke`/`astream`，同步 SqliteSaver 的 async 方法直接 `NotImplementedError`——**图能 compile ≠ 能跑**，编译期完全不暴露。这个 bug 是给合规回路写的图级测试（FakeLLM 驱动真实执行）第一次跑就抓出来的。引申：LLM 系统的测试策略——单元测不了的，就 mock LLM 在图级别测行为。

**Q17：SSE vs WebSocket 怎么选的？**
> 要点：单向推送为主 → SSE；基于 HTTP、自动重连、代理友好；`astream(stream_mode="updates")` 天然是事件生成器，和 SSE 的 `data:` 帧一一对应。需要双向（如人工坐席接管）再上 WebSocket。

**Q18：CI 里为什么要给 DASHSCOPE_API_KEY 一个占位值？**
> 要点：`api.main` 模块级构造 `DashScopeEmbeddings`，key 为空则 import 即炸——本机有 `.env` 掩盖了问题，CI 第一次跑就暴露。根因是**模块级副作用**（全局单例在 import 时初始化），正确做法是 lifespan 里装配；占位值是务实止血。

**Q19：docker compose 的启动顺序怎么保证？**
> 要点：MySQL/Redis 配 healthcheck，app 服务 `depends_on: condition: service_healthy`——光等容器 started 没用，MySQL ready 前应用连不上会崩。app 自己还有 `self_check()` 二次保险。

### 🎤 开放题（没有标准答案，考的是思路）

**Q20：QPS 涨 100 倍，先改什么？**
> 参考思路：进程内组件全换共享存储（限流/working memory → Redis）→ uvicorn 多 worker + Redis 已就绪 → LLM 调用是吞吐瓶颈，确定性路由命中率要提高 + 合规 LLM 检查采样化 → SQLite 换 PostgreSQL（checkpoint/幂等都有并发写）→ 上消息队列削峰。

**Q21：怎么知道这个系统"回答得好"？**
> 参考思路：三层——①图级单测保行为正确（已有 21 用例）；②评测脚本保质量（`evaluation/` 路由准确率/RAG 质量/合规拦截，对着标注用例集跑）；③线上指标（OTel 记录每 Agent 调用、合规拦截率、重写次数）+ 用户负反馈。没有标注集就先人工标 100 条。

**Q22：新加一个"退款执行 Agent"要动哪些地方？**
> 参考思路：`agents/` 加实现 → `ALLOWED_INTENTS` 加标签 → supervisor 提示词和 `_deterministic_intent` 加路由词 → conditional edge 映射表加一条 → 合规工具结果上下文把它加进 `tool_results` → 评测脚本加用例。能一口气说全，说明真的读过这张图。

## 五、三分钟讲清这个项目（面试开场话术）

> "这是一个多 Agent 客服系统，我想重点讲三个工程决策。第一是**合规修复回路**：回答出站前过'规则+LLM'双重检查，不合规的不是拦截而是带违规原因重写，最多两次，耗尽降级兜底——在 LangGraph 里就是一条 conditional edge 构成的环。第二是**幂等**：工单幂等键用用户加内容指纹而不是请求 ID，防得住重试也防得住重复提交，并发下靠主键约束兜底。第三是**降级链**：Redis 挂了走 MySQL，MySQL 挂了走进程内，但 MySQL 本身是硬依赖、启动自检失败就拒绝启动——每个组件的失效语义都是显式设计的。这三个决策都写了测试，其中图级测试还真实抓出过一个 checkpointer 的异步兼容 bug。"

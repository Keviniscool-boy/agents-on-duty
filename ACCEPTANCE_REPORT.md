# 产品化修复验收报告（feat/product-fix）

- **日期**：2026-11-04
- **基线**：`main` = `bbdd5e6`（"fix: expose handoff tickets and improve fallback responses"）
- **分支**：`feat/product-fix`（10 个本地 commit，**未 push**）
- **工作树**：`C:\Users\Kevin\Desktop\agent-on-duty\agents-on-duty`
- **结论**：goal 六项要求全部有代码、回归测试与实测证据；全量后端测试 222 通过 / 2 环境跳过 / 0 失败；浏览器双角色实测 16 项通过。

---

## 1. 提交清单

| Commit | 内容 |
|---|---|
| `112db2a` | 运维接口后端角色校验 + 登出吊销 Bearer/Cookie + conftest 环境隔离 + 权限矩阵测试 |
| `3c4d671` | 确定性转人工：supervisor 路由、直建工单、`GET /api/tickets`、售后页「我的工单」、闭环测试 |
| `a8c53c3` | 合规韧性：LLM 检查 15s 超时/异常降级；固定话术跳过合规模型（仍走规则检查） |
| `3c591b9` | 前端：角色导航、安全 Markdown、订单卡片、失败重试、AfterSales 修正 + XSS 测试 |
| `b45e4a3` | 固定业务话术经 synthesizer 原样透传（不再被 LLM 重排） |
| `c9fa9d4` | 订单卡片同时从 order_list 结果提取（含 `_finalize_chat` 单测） |
| `372e5e8` | 前端连接/流式看门狗（后端挂起时 21s 内报错并可重试） |
| `eecec1c` | 工单携带认证用户名（工作台显示客户名）；窄屏 tab 横向滑动 |
| `aae4a5d` | 补齐 username 贯通漏写的两处（chat 初始状态、tools/call、工作台详情） |
| `d4c0585` | README 权限矩阵/转人工说明、roadmap 阶段七 |

**改动规模**：26 个文件，+1361 / −75。未混入用户已有改动（开工时工作区干净）。

## 2. 权限矩阵（后端实测）

| 接口 | 未认证 | 普通用户 | 客服(agent) | 管理员(admin) |
|---|---|---|---|---|
| `/api/chat*`、`/api/orders`、`/api/refunds`、`/api/tickets` | 401 | ✅ 仅本人数据 | ✅ | ✅ |
| `/api/tools`、`/api/tools/call`、`/api/metrics` | 401（实测） | **403（实测）** | ✅（实测） | ✅（实测） |
| `/api/workbench/*` | 401 | 403 | ✅ | ✅ |
| `/api/admin/*` | 401 | 403 | 403 | ✅ |

- 登出：Bearer 与 Cookie 任一携带方式都会吊销实际令牌；两者同时存在时全部吊销（不留可用残余，测试固化该语义）。
- 测试文件：`test_permission_matrix.py`（13 用例）、`test_auth_hardening.py`。

## 3. 测试与构建结果

| 项目 | 命令 | 结果 |
|---|---|---|
| 后端全量 | `python -m pytest`（MySQL/Redis 指向本机测试实例） | **223 passed, 2 skipped, 0 failed** |
| 前端测试 | `npm test`（node --test） | **14 passed**（adminData 5 + markdown XSS 9） |
| 前端构建 | `npm run build` | ✅ 24 modules，无告警 |
| 秘密扫描 | `git diff main` 全文检索 key/password/secret/token | 仅代码标识符，无真实凭据 |

- 2 个 skip：`test_concurrency.py`、`test_mysql_fallback.py` 各 1 项，需要真实 MySQL（与项目 CI 同款要求；本机 3307 测试实例未运行）。属环境依赖，非回归。
- 本轮新墔回归测试：权限矩阵 13、转人工/工单闭环 12、合规韧性 9、合成透传 2、订单卡片提取 1、XSS 9。

## 4. 浏览器双角色实测（http://localhost:8080，docker compose 全新构建）

| # | 验收项 | 结果 |
|---|---|---|
| 1 | Kevin（普通用户）导航：无「运维面板/工作台/管理员」入口 | ✅ |
| 2 | 问候「你好」 | ✅ 1.06s 返回固定话术 |
| 3 | 「转人工」 | ✅ 1.25s，固定话术含工单号 TK-46618A1D 与状态 |
| 4 | 售后页「我的工单」区块 | ✅ 编号/状态/创建时间/处理客服 |
| 5 | 已取消订单 | ✅ 无退款按钮，显示"已取消订单不支持退款申请" |
| 6 | 普通用户直调 `/api/metrics`、`/api/tools`（页面内 fetch） | ✅ 双 403 |
| 7 | admin 导航：5 个入口可见、文字固定无重复 | ✅ |
| 8 | 客服工作台视图无聊天侧栏 | ✅ |
| 9 | 工作台「人工工单」队列看到 2 张新工单（含客户名） | ✅ |
| 10 | 工单状态流转 待处理→处理中→已解决 | ✅ |
| 11 | Kevin 侧同步看到"已解决"+ 处理客服 admin | ✅ |
| 12 | 运维面板加载工具/指标 | ✅ |
| 13 | 快捷按钮点击立即发送 | ✅ |
| 14 | 安全 Markdown 渲染（列表 + 行内 code） | ✅ |
| 15 | 订单卡片（order_list 结构化结果） | ✅ 2 张卡片：商品/状态/金额/签收时间 |
| 16 | 知识问答（退货政策） | ✅ "7×24 小时"+ 引用来源片段 |
| 17 | agent 角色导航（临时账号 agent_test） | ✅ 见工作台/售后/会话/运维面板，无"管理员"入口 |
| 18 | agent 角色工单队列 | ✅ 可见全部待处理工单（含计时产生的新单） |
| 19 | 审计可观测 | ✅ `/api/admin/audit?kind=tool` 含 `intent=handoff` 建单事件 |

**失败重试实测**（docker pause 模拟后端故障）：
- 修复前：界面永远停在"正在连接"（缺陷，已修复 commit `372e5e8`）。
- 修复后：**21 秒**出现"连接超时或中断，请点击重试" + 用户消息保留 + 重试按钮；恢复后端后点重试正常返回回答。

## 5. 性能测量方法与限制

- 方法一（端到端）：浏览器自动化从点击发送到目标气泡出现的计时，`Date.now()` 差值；服务为新构建容器。问候 1.06s、转人工 1.25s。
- 方法二（可重复，N=5）：页面内 fetch 直连 `POST /api/chat`（排除浏览器渲染开销），各 5 次：
  - **问候：819–952ms（全 200）**，序列 952/819/861/833/853；
  - **转人工：734–831ms（全 200）**，序列 734/785/831/811/780。
  - 环境：本机 docker compose（app/frontend/mysql/redis 同机）、新会话每次、2026-11-04。
- 两条路径均为零模型调用（supervisor 确定性路由 → 规则合规 → 透传合成），开销 = 网络 + SQLite 写入；样本仍限于本机单实例，不构成生产性能结论。
- 未宣称任何模型延迟优化：`MODEL_NAME` 保持 `gpt-6-luna`（已在 ztoken 模型清单确认有效；清单共 15 个聊天/图像模型）。切换模型需要多轮真实调用对比，按"控制 token 用量"约束未执行。
- 计时与 agent 验证产生的测试数据：新增若干演示工单（工作台可见）与临时账号 `agent_test`（角色 agent），均为演示库数据。
- 订单查询走模型（routing + 合成），实测正常出卡片，但延迟未作为指标记录。

## 6. 环境限制与已知问题

1. **OTEL exporter 噪音**：容器日志有 `localhost:4317 connection refused`（无 OTLP 后端），不影响功能。
2. **一次性知识库拒答**：pause/unpause 容器后的首次知识问答出现一次"没有相关信息"；容器内直接检索同题命中 0.727 分。归因为容器恢复瞬时的单次异常，非稳定缺陷；后续复测正常。
3. **`/health` 显示 `knowledge_base.status: unknown`**：embedding 可用性只在首次检索时探测（现状设计），检索本身正常。
4. **历史遗留工单**：工作台有 20+ 张旧测试工单（user_id="旧工单"），来自此前测试数据，非本轮产生。
5. **pi 对比实验**：真实模型端到端对比仍未执行（roadmap 已如实标注）。
6. goal 提到的「RF-ID 折叠」：代码中不存在 RF-ID 字段/文案（audit 所指为演示数据），无可实施项；已取消订单的退款入口已按要求隐藏。

## 7. 未完成项与剩余风险

- **未 push**：10 个 commit 均在本地 `feat/product-fix`，等待审阅后合并/推送。
- ~~Agent 图级超时测试~~ 已补：`test_compliance_timeout_flows_through_whole_graph` 验证合规 LLM 超时降级后整图继续合成、无未定义状态（role-aware LLM 桩 + FakeKB，不依赖真实模型）。
- **幂等键兼容性**：`build_idempotency_key` 指纹新增 `session_id` 字段，新工单与旧工单键空间不同（仅影响新记录，无迁移问题）。
- **多账号压测/性能基准**：不在本轮范围（goal 明确排除）。
- **ChatView 组件复杂度**：导航/视图切换逻辑集中在一个组件，可读性尚可但值得后续拆分。

## 8. 保守假设记录

- 角色名核实自代码：`user / agent / admin`（`auth/database.py` 白名单 + `require_role` 用法）。
- 工单查询的客服语义：agent/admin 调 `GET /api/tickets` 也只返回自己的工单（工作台队列走 `/api/workbench/tickets`），测试固化。
- 转人工幂等按会话隔离：同一会话重试返回同单，跨会话各建新单。
- 合规模型超时降级为"规则结果说了算"，并在 `sub_results.compliance` 标记 `degraded`/`llm_skipped` 供审计区分；JSON 非法仍按保守拒绝（不降级），维持原有安全语义。
- 前端流式看门狗阈值：连接期 20s、流期静默 90s（低于服务端 120s 兜底，且大于节点级最长间隔）。

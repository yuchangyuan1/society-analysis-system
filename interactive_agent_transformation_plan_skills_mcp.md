# 社会分析 AI Agent 交互化改造方案（Tools / Capabilities 分层版 · 修订 2026-04-21）

## 0. 本次修订说明

本文在原 *Skills / MCP 集成版* 基础上，结合对现有代码的实际审计进行修订。核心变化如下（保留了原方案的分层理念与 6 种能力设计）：

| 原版提法 | 修订后提法 | 原因 |
|---|---|---|
| Skill Layer，目录 `skills/` | **Capability Layer，目录 `capabilities/`** | 项目根目录 `skills/` 已存在 17 个 Claude Code markdown skill pack（claim-retrieve、emotion-analyze 等），和 Python skill 模块目录名冲突 |
| MCP Tool Layer（未说明协议） | **Tools Layer（普通 Python service 模块）** | 首版不做真 Anthropic MCP 协议（独立进程、JSON-RPC、tool manifest）。首版用 Pydantic schema + 普通函数调用，将来可选包一层真 MCP server |
| 25 个 MCP Tool | **12 个 Tool** | `trigger_refresh` / `load_session_context` 等 5 个与后台任务 / 会话持久化强耦合，留 Phase 4；`retrieve_topic_background` 并入 `retrieve_official_sources` |
| "Social Graph Store" | **内存重建的 NetworkX 图** | 不上 Neo4j / memgraph。从 `report_raw.json` 的 `propagation_summary` + `community_analysis` 即时重建图对象，查询时间 < 1s |
| Phase 1 = Skills、Phase 2 = MCP | **垂直切片：每个 Skill 和它用到的 Tool 一起落地** | 原顺序下 Skill 依赖尚未存在的 Tool，Phase 1 末尾无法跑通 |
| 默认所有 Agent 都新写 | **保留 `agents/` 全部现有模块；只新增 Router / ChatOrchestrator / AnswerComposer 三个** | 现有 10 个 agent（planner、ingestion、knowledge、analysis、community、risk、visual、report、counter_message、critic）继续被 Precompute Pipeline 调用，不改 |
| 未提数据模型改动 | **`IncidentReport.claims: list[str]` 必须扩展为 `list[Claim]` 或新增字段** | 目前结构化 Claim 对象不落盘，`report_raw.json` 只有 claim 字符串，ClaimStatusCapability / `get_claims` 无从读取 |

---

## 1. 方案定位

将当前 **run-centric 的离线批处理系统** 升级为 **session-centric、capability-driven、tool-enabled 的交互式社会分析 AI Agent**。

核心判断：

- **Agent** 负责意图路由 / 会话编排 / 多 Capability 组合；
- **Capability** 承载稳定的领域任务闭环（用户可感知的能力单元）；
- **Tool** 承载原子化、可复用、可审计的底层查询 / 生成能力；
- 现有离线 pipeline（`agents/planner.py` 主干 + ingestion/knowledge/analysis/community/risk/visual/report/counter_message/critic）退居为 **Data & Precompute Layer**，不被推翻；
- 现有 `data/runs/{run_id}/` 5 件 artefact 从"最终交付物"升级为"Capability 的底层可查询状态"。

一句话概括：

> **Agent 负责决策与编排，Capability 负责任务闭环，Tool 负责底层查询。**

---

## 2. 为什么引入三层（简述）

- **复用性**：同一个"热点概览"能力可被 chat、dashboard、daily briefing 三种前端复用；
- **可测试性**：Tool 层是纯函数级查询，Capability 层返回结构化对象，都可以单元测试；
- **可替换性**：Tool 实现替换（例如 Chroma 换成 pgvector）不会影响 Capability；
- **答辩叙事**：可以清晰地说明"Agent + Capability + Tool + Knowledge Layer 分层架构"，比"一堆 agent"专业度明显更高。

---

## 3. 总体架构

```
┌─────────────────────────────────────────────────────────┐
│  UI / API Layer                                          │
│  - Streamlit Chat page（新）                             │
│  - Run Detail / Run List pages（保留）                   │
│  - POST /chat/query, POST /capabilities/*（新）           │
│  - GET /runs, /runs/{id}/*（保留）                       │
└─────────────────────────────────────────────────────────┘
                           │
┌─────────────────────────────────────────────────────────┐
│  Agent / Orchestration Layer（新）                       │
│  - chat_orchestrator.py                                  │
│  - router.py                                             │
│  - answer_composer.py                                    │
│  - session_store.py（JSON 文件，首版不上 DB）             │
└─────────────────────────────────────────────────────────┘
                           │
┌─────────────────────────────────────────────────────────┐
│  Capability Layer（新 — capabilities/）                   │
│  - topic_overview_capability.py                          │
│  - emotion_insight_capability.py                         │
│  - claim_status_capability.py                            │
│  - propagation_insight_capability.py                     │
│  - visual_summary_capability.py                          │
│  - run_compare_capability.py                             │
└─────────────────────────────────────────────────────────┘
                           │
┌─────────────────────────────────────────────────────────┐
│  Tools Layer（新 — tools/，Python service 模块）          │
│  - run_query_tools.py                                    │
│  - evidence_tools.py                                     │
│  - graph_tools.py （内存 NetworkX）                       │
│  - decision_tools.py                                     │
│  - visual_tools.py（调现有 agents/visual.py）             │
└─────────────────────────────────────────────────────────┘
                           │
┌─────────────────────────────────────────────────────────┐
│  Knowledge & State Layer（扩展，不重写）                  │
│  - Run Store: data/runs/{run_id}/*.json（已有）          │
│  - Evidence RAG: services/chroma_service.py（已有）      │
│  - Social Graph: 内存重建（不上独立 DB）                  │
│  - Session State: data/sessions/{session_id}.json（新）  │
└─────────────────────────────────────────────────────────┘
                           │
┌─────────────────────────────────────────────────────────┐
│  Data & Precompute Layer（全保留）                        │
│  - agents/planner.py 定位改为 "Precompute Pipeline"      │
│  - agents/ingestion / knowledge / analysis / community / │
│    risk / visual / report / counter_message / critic     │
│  - 触发方式：定时任务 + POST /runs（Phase 4 再做）        │
└─────────────────────────────────────────────────────────┘
```

---

## 4. 职责边界

### 4.1 Agent 做什么

- 理解用户问题（`router.py`）
- 识别意图、目标对象（run_id / topic_id / claim_id / time_window）
- 决定调用哪个 Capability、是否多 Capability 组合
- 管理会话状态（当前 run / 最近 topic / 最近 visual）
- 汇总 Capability 输出 → 通过 `answer_composer.py` 合成用户可读回答

**禁止**：Agent 直接读 `data/runs/*.json`、直接调 `chroma_service`、直接拼 LLM retrieval prompt。

### 4.2 Capability 做什么

- 一个完整领域任务闭环（对应用户能感知的能力）
- 内部调用 1 个或多个 Tool
- 返回结构化 Pydantic 对象（**不是**原始 LLM 文本）
- 可在多 Agent / 多前端复用

### 4.3 Tool 做什么

- 原子化操作（查询 / 生成 / 合并）
- 明确 Pydantic 输入输出 schema
- 可独立单元测试
- 屏蔽底层存储细节

---

## 5. Capabilities 设计（6 个）

每个 Capability 应包含：`class <Name>Capability` + `class <Name>Input(BaseModel)` + `class <Name>Output(BaseModel)` + `def run(input) -> output` 方法。

### 5.1 TopicOverviewCapability

| 字段 | 内容 |
|---|---|
| 典型问题 | "今天讨论了什么热点"、"哪几个主题最热" |
| Input | `run_id: str = "latest"`, `top_k: int = 5`, `sort_by: Literal["post_count","velocity","misinfo_risk"] = "post_count"` |
| Output | `run_id`, `topics: list[TopicBrief]`，`TopicBrief = {topic_id, label, post_count, velocity, dominant_emotion, is_trending, misinfo_risk, representative_claims[:2]}` |
| 调用 Tools | `get_run_summary`, `get_topics` |
| LLM 参与 | 可选 — 一句话概括各 topic（temperature≤0.3） |
| 复用/新写 | **~95% 复用** `models/report.py::TopicSummary`，只做字段截取 |

### 5.2 EmotionInsightCapability

| 字段 | 内容 |
|---|---|
| 典型问题 | "情绪怎么样"、"哪些话题最愤怒" |
| Input | `run_id`, `topic_id: Optional[str] = None` |
| Output | `overall_emotion_distribution: dict[str,float]`, `dominant_emotion: str`, `topic_emotions: list[{topic_id, dominant_emotion, emotion_distribution}]`, `interpretation: str` |
| 调用 Tools | `get_topics`（已含 emotion 字段） |
| LLM 参与 | 仅生成 `interpretation` 的一句话 |
| 复用/新写 | **95% 复用**；`TopicSummary.emotion_distribution` + `dominant_emotion` 已在 `agents/analysis.py` 落盘 |

### 5.3 ClaimStatusCapability

| 字段 | 内容 |
|---|---|
| 典型问题 | "哪些说法证据不足"、"为什么 abstain"、"这个 claim 有证据吗" |
| Input | `run_id`, `topic_id: Optional[str]`, `claim_id: Optional[str]` |
| Output | `claims: list[ClaimStatus]`, `primary_claim_id`, `intervention_decision`，`ClaimStatus = {claim_id, normalized_text, actionability, non_actionable_reason, supporting_count, contradicting_count, uncertain_count, evidence_tiers, verdict_label: "supported" \| "contradicted" \| "insufficient" \| "non_factual"}` |
| 调用 Tools | `get_claims`, `get_claim_details`, `retrieve_evidence_chunks`（按需），`get_intervention_decision` |
| LLM 参与 | 合成 `verdict_summary` 的一段文字 |
| 复用/新写 | **80% 复用**：`Claim.evidence_summary()`、`claim_actionability`、`non_actionable_reason`、`InterventionDecision` 全在模型里。**需要数据层改动：`IncidentReport.claims` 从 `list[str]` 改为带结构化 Claim 的存储（见 §10）** |
| 输出口径 | 统一使用 `supported / contradicted / insufficient / non_factual`；禁止"绝对真/假" |

### 5.4 PropagationInsightCapability

| 字段 | 内容 |
|---|---|
| 典型问题 | "谁带起来的"、"有没有 bridge 账号"、"为什么 bridge=0" |
| Input | `run_id`, `topic_id: Optional[str]` |
| Output | `account_role_counts: dict`, `bridge_influence_ratio: float`, `role_risk_correlation: Optional[float]`, `community_count: int`, `coordinated_pairs: int`, `interpretation: str` |
| 调用 Tools | `query_topic_graph`, `get_run_metrics` |
| LLM 参与 | 仅 `interpretation` |
| 复用/新写 | **98% 复用**：`PropagationSummary`、`metrics.json` 全有；`interpretation` 可先用固定模板（非 LLM） |

### 5.5 VisualSummaryCapability

| 字段 | 内容 |
|---|---|
| 典型问题 | "用一张图总结这个主题"、"为什么不能出图" |
| Input | `run_id`, `topic_id: str`, `preferred_visual_type: Optional[Literal["rebuttal","evidence_context"]] = None` |
| Output | `visual_type: str`, `image_path: Optional[str]`, `abstention_block: Optional[str]`, `decision_reason: str` |
| 调用 Tools | `get_primary_claim`, `get_claim_details`, `retrieve_evidence_chunks`, `get_intervention_decision`, `generate_visual_card` |
| LLM 参与 | 无（visual.py 内部模板 + PIL） |
| 复用/新写 | **100% 复用** `agents/planner.py` Step 9 分支逻辑 + `agents/visual.py::generate_rebuttal_card` / `generate_evidence_context_card`。核心工作是**把 Step 9 分支提取成独立函数** |
| 核心规则 | decision 先于 visual；`abstain` 输出结构化文本块，不硬出 PNG |

### 5.6 RunCompareCapability

| 字段 | 内容 |
|---|---|
| 典型问题 | "这次和上次比"、"哪些指标变化大" |
| Input | `run_id_a: str`, `run_id_b: Optional[str] = "previous"` |
| Output | `changes: list[MetricChange]`，`MetricChange = {metric, a_value, b_value, delta, significance: "up"\|"down"\|"flat"}`, `narrative: str` |
| 调用 Tools | `list_runs`, `get_run_summary`, `get_run_metrics` |
| LLM 参与 | `narrative` 的一段话 |
| 复用/新写 | **完全新写**（~150 行）— 项目里没有任何跨 run 对比工具 |

---

## 6. Tools 设计（12 个）

所有 Tool 都是 `tools/*.py` 里的普通 Python 函数，导出 `def tool_name(input: PydanticModel) -> PydanticModel`。

### 6.1 Run Query 类（4 个 → `tools/run_query_tools.py`）

| Tool | 复用 |
|---|---|
| `list_runs` | `api/routes/runs.py::list_runs()` 现有逻辑直接搬，输出加 `source: Literal["data","sample"]` 字段（区分 `data/runs/` vs `sample_runs/`） |
| `get_run_summary` | `api/routes/runs.py::get_run()` |
| `get_topics` | 读 `report_raw.json::topic_summaries`，序列化 |
| `get_claims` / `get_claim_details` / `get_primary_claim` | 读 `report_raw.json::claims`（**需 §10 的模型改动**） + `intervention_decision.primary_claim_id` lookup |

### 6.2 Evidence 类（2 个 → `tools/evidence_tools.py`）

| Tool | 复用 |
|---|---|
| `retrieve_evidence_chunks` | `services/chroma_service.py::query_articles`。把 `agents/knowledge.py:323-365` 的 per-claim Chroma 查询逻辑抽成独立函数（不耦合 Claim 对象） |
| `retrieve_official_sources` | 组合 `services/wikipedia_service.py::fetch_summary` + `services/news_search_service.py`。输入加 `source_scope: Literal["official","all"] = "all"` |

### 6.3 Graph / Social 类（2 个 → `tools/graph_tools.py`）

| Tool | 复用 |
|---|---|
| `query_topic_graph` | **新写 ~120 行**：从 `report_raw.json::community_analysis.communities` + `propagation_summary` + 原始 posts（可选）在请求时重建 NetworkX `Graph` 对象，缓存在 `functools.lru_cache(maxsize=8)`（per run_id）。返回 `{node_count, edge_count, bridge_accounts, community_counts, originator_accounts}` |
| `get_social_metrics` | 直接读 `metrics.json` 的 `bridge_influence_ratio` / `role_risk_correlation` / `account_role_counts` / `community_modularity_q` |

### 6.4 Decision / Visual 类（3 个 → `tools/decision_tools.py` + `tools/visual_tools.py`）

| Tool | 复用 |
|---|---|
| `get_intervention_decision` | 读 `report_raw.json::intervention_decision` |
| `generate_visual_card` | thin wrapper 调 `agents/visual.py::generate_rebuttal_card` 或 `generate_evidence_context_card`，返回 `{image_path, visual_type}` |
| `get_counter_effect_history` | 调 `services/counter_effect_service.py::get_effect_report()` |

**Phase 4 再追加**：`trigger_refresh`, `load_session_context`, `save_session_context`, `render_topic_snapshot`, `retrieve_topic_background`。

---

## 7. Agent 层设计

### 7.1 结构

**首版只 3 个 agent**（轻 Agent 重 Capability）：

- `agents/router.py` — LLM 意图分类 + 解析 run/topic/claim 目标
- `agents/chat_orchestrator.py` — 会话入口 + session state 管理 + Capability 调度
- `services/answer_composer.py` — 把 Capability 输出合成为用户可读回答

### 7.2 Router 意图分类

| intent | 对应 Capability |
|---|---|
| `topic_overview` | TopicOverviewCapability |
| `emotion_analysis` | EmotionInsightCapability |
| `claim_status` | ClaimStatusCapability |
| `propagation_analysis` | PropagationInsightCapability |
| `visual_summary` | VisualSummaryCapability |
| `run_compare` / `run_navigate` | RunCompareCapability |
| `explain_decision` | ClaimStatusCapability + 强制附带 `intervention_decision` |
| `followup` | 读 session state，不重新分类，默认继续上一 Capability |

Router 输出 schema：
```python
class RouterOutput(BaseModel):
    intent: Literal[...]
    targets: {run_id, topic_id, claim_id}  # 全可选
    confidence: float
    fallback_reason: Optional[str]  # 如果 confidence < 0.5
```

### 7.3 Chat Orchestrator 主链路

```
POST /chat/query
  ↓
load session state（session_id）
  ↓
router.classify(user_message, session_context)
  ↓
capability = REGISTRY[intent]
  ↓
capability_output = capability.run(input)
  ↓
answer_composer.compose(user_message, capability_output, session_context)
  ↓
update session state
  ↓
return {answer_text, capability_output_raw, visual_paths}
```

---

## 8. 数据层与知识层

### 8.1 Run Store（已存在）

`data/runs/{run_id}/` 的 5 件 artefact 不动。Tool 层统一通过 `tools/run_query_tools.py` 暴露，Capability 层不直接读文件。

### 8.2 Evidence RAG（已存在）

`services/chroma_service.py` 不动。原来只在 `agents/knowledge.py` 内部用，现在通过 `tools/evidence_tools.py::retrieve_evidence_chunks` 对外暴露。

### 8.3 Social Graph（内存重建）

**不上图数据库**。`tools/graph_tools.py::query_topic_graph` 按 `run_id` 从 `report_raw.json` + （若需要）原始 `posts.json`（若 ingestion 时落盘）重建 NetworkX `Graph`，用 `lru_cache` 保持热。典型 run 500-1700 communities，构图 < 1s，够用。

Phase 4 若真有性能问题再考虑 memgraph / neo4j。

### 8.4 Session State（新，JSON 文件）

```python
data/sessions/{session_id}.json:
{
  "session_id": str,
  "created_at": datetime,
  "current_run_id": Optional[str],
  "current_topic_id": Optional[str],
  "current_claim_id": Optional[str],
  "recent_visuals": list[str],
  "conversation": list[{role, content, capability_used, at}]
}
```

`services/session_store.py` 提供 `load / save / append_turn` 三个方法。首版单机，不考虑并发锁。

---

## 9. 模块复用与继承映射（审计摘要）

### 9.1 可直接复用（不改代码，只改调用方式）

| 现有模块 | 新角色 |
|---|---|
| `agents/planner.py`（整个 PlannerAgent） | 降级为 Precompute Pipeline，被定时任务 / CLI / 将来的 `POST /runs` 调用，不被 chat 路径调用 |
| `agents/ingestion.py` / `knowledge.py` / `analysis.py` / `community.py` / `risk.py` / `report.py` / `counter_message.py` / `critic.py` | 保持不动，仍被 planner 调用 |
| `agents/visual.py::generate_rebuttal_card` / `generate_evidence_context_card` | 被 `tools/visual_tools.py` 调用 |
| `services/actionability_service.py` / `intervention_decision_service.py` | 不动，planner 和 ClaimStatusCapability 都调 |
| `services/metrics_service.py` / `manifest_service.py` | 不动 |
| `services/chroma_service.py` | 不动，新增入口经 `tools/evidence_tools.py` |
| `services/wikipedia_service.py` / `news_search_service.py` | 不动，被 `tools/evidence_tools.py` 组合 |
| `services/counter_effect_service.py` | 经 `tools/decision_tools.py::get_counter_effect_history` 暴露 |
| `api/app.py` + `api/routes/runs.py` / `artifacts.py` | 全保留。新增 `api/routes/chat.py` + `api/routes/capabilities.py` |
| `ui/pages/1_Run_List.py` / `2_Run_Detail.py` / `ui/components/*.py` | 全保留（改名为"Research"视图）；新增 `ui/pages/0_Chat.py` 作为默认主入口 |
| `sample_runs/run_fixed_claims_baseline/` / `run_live_demo/` | 不动，chat 可以直接用 `run_id=run_fixed_claims_baseline` 查固定数据做 demo |
| 现有 `skills/*/SKILL.md`（Claude Code skill pack） | 不动，属于不同层的产物 |

### 9.2 现有模块的小幅扩展

| 模块 | 扩展 |
|---|---|
| `models/report.py::IncidentReport` | **`claims: list[str]` → `claims: list[Claim]`**（或新增 `structured_claims` 字段），让 `report_raw.json` 带结构化 Claim。见 §10 |
| `agents/report.py` | 消费新的 `claims` 字段时同步修改渲染（影响 `_render_body` 里 `for c in claims`） |
| `agents/planner.py::_build_report_context` | 确保结构化 Claim 传到 report_agent，落盘 |

---

## 10. 必要的数据模型改动（关键）

当前状态：

```python
# models/report.py
class IncidentReport(BaseModel):
    claims: list[str] = Field(default_factory=list)  # ← 只存字符串
    ...
```

但结构化的 `Claim`（含 `claim_actionability`、`non_actionable_reason`、`supporting_evidence` 等）**不落盘**。`report_raw.json` 里只有 claim 字符串。

### 改法（二选一）

**方案 A（推荐）**：改 `IncidentReport.claims: list[Claim]`。优点：只有一个字段；缺点：破坏已有 `report_raw.json` 的 schema，sample_runs 需重新生成。

**方案 B**：新增 `structured_claims: list[Claim] = Field(default_factory=list)`，`claims: list[str]` 保留向后兼容。优点：不破坏旧 artefact；缺点：两个字段并存，渲染需同步修改。

**决策**：推荐方案 A。`sample_runs/run_fixed_claims_baseline/` 和 `run_live_demo/` 都才创建几小时，重跑代价低。`final_project_transformation_plan.md` 等历史档案不依赖 `report_raw.json` 具体 schema。

### 影响面

- `models/report.py`：类型注解改 `claims: list[Claim]`
- `agents/planner.py`：现在会把 `[c.normalized_text for c in unique_claims]` 赋给 `report.claims`，改为直接赋 `unique_claims`
- `agents/report.py`：`_render_body` 和 `_render_template` 里所有 `claims` 迭代需取 `c.normalized_text`
- `services/metrics_service.py`：`_claim_has_any_evidence` 已经按 `Claim` 对象写的，不变
- **验收**：重跑 fixture → 打开 `report_raw.json` 能看到 claims 数组里有 `claim_actionability` / `supporting_evidence` 等字段

---

## 11. 修订后的目录结构

```text
society-analysis-project-update/
  agents/                          # 保留
    planner.py                     # 降级为 Precompute Pipeline
    ingestion.py / knowledge.py / analysis.py / community.py /
    risk.py / report.py / visual.py / counter_message.py / critic.py
    router.py                      # 新
    chat_orchestrator.py           # 新

  capabilities/                    # 新（不是 skills/）
    __init__.py
    base.py                        # Capability 基类 + registry
    topic_overview_capability.py
    emotion_insight_capability.py
    claim_status_capability.py
    propagation_insight_capability.py
    visual_summary_capability.py
    run_compare_capability.py

  tools/                           # 新（不是 mcp_tools/）
    __init__.py
    base.py                        # Tool 基类 + 统一异常
    run_query_tools.py
    evidence_tools.py
    graph_tools.py
    decision_tools.py
    visual_tools.py

  services/                        # 保留 + 新增
    chroma_service.py              # 保留
    actionability_service.py       # 保留
    intervention_decision_service.py # 保留
    metrics_service.py / manifest_service.py / counter_effect_service.py
    wikipedia_service.py / news_search_service.py / reddit_service.py / telegram_service.py
    answer_composer.py             # 新
    session_store.py               # 新

  api/                             # 扩展
    app.py
    routes/
      runs.py                      # 保留
      artifacts.py                 # 保留
      chat.py                      # 新：POST /chat/query
      capabilities.py              # 新：POST /capabilities/<name>

  ui/                              # 扩展
    streamlit_app.py
    api_client.py
    pages/
      0_Chat.py                    # 新，默认主入口
      1_Run_List.py                # 保留
      2_Run_Detail.py              # 保留（改名 Research 视图）
    components/
      metric_cards.py / community_graph.py / emotion_chart.py  # 保留
      chat_response.py             # 新，渲染 capability 输出

  models/                          # 扩展
    claim.py / report.py / manifest.py / ...  # 保留
    session.py                     # 新
    chat.py                        # 新：ChatMessage / ChatQuery / ChatResponse

  skills/                          # 不动，Claude Code markdown skill pack

  data/
    runs/                          # 已有
    sessions/                      # 新

  sample_runs/                     # 不动
  docs/                            # 不动（demo_script 之后改）
  tests/                           # 扩展 — capability / tool 单测

  main.py                          # Precompute CLI，不动
```

---

## 12. 工作流

### 12.1 Precompute Pipeline（保留现状）

触发方式：
- CLI：`python main.py --subreddit X --days N`（保留）
- CLI：`python main.py --claims-from fixture.json`（保留）
- Phase 4：定时任务 + `POST /runs`

功能：原 24 stage pipeline 全保留，产出 `data/runs/{run_id}/*`。

### 12.2 问答 Pipeline（新）

```
[UI Chat page]
  → POST /chat/query {session_id, message}
    → ChatOrchestrator.handle(message)
      → SessionStore.load(session_id)
      → Router.classify(message, session_context) → {intent, targets}
      → Capability = REGISTRY[intent]
      → Capability.run(input) → structured output
        ↓
        Capability 内部 → Tool.run() → ...
        ↓
      → AnswerComposer.compose(user_msg, capability_output, session_context)
      → SessionStore.append_turn(...)
      → return {answer_text, structured_output, visuals}
    → UI 渲染聊天消息 + 右侧面板展示证据 / 图卡
```

### 12.3 三个典型例子

**例 1：热点概览**
```
用户："今天讨论了什么热点"
→ Router: topic_overview
→ TopicOverviewCapability.run(run_id="latest", top_k=5)
  → get_run_summary → get_topics
→ AnswerComposer 合成 "Top 5 主题："
```

**例 2：claim 判断**
```
用户："Rothschild 这个说法有证据吗"
→ Router: claim_status, claim_id=... (LLM 解析目标)
→ ClaimStatusCapability.run(...)
  → get_claim_details → retrieve_evidence_chunks → get_intervention_decision
→ AnswerComposer 合成 "verdict: insufficient / supported / contradicted"
```

**例 3：图卡总结**
```
用户："给我用一张图总结 Joe Rogan 主题"
→ Router: visual_summary, topic_id=...
→ VisualSummaryCapability.run(...)
  → get_primary_claim → get_claim_details → retrieve_evidence_chunks
  → get_intervention_decision
  → decision==rebut → generate_visual_card(type="rebuttal")
  OR decision==abstain → 返回 abstention_block 文本
→ UI 展示 PNG 或结构化说明
```

---

## 13. 分阶段实施计划（垂直切片）

每个 Phase 都跑通端到端。

### Phase 0 · 架构打底（2-3 天）

**目标**：结构骨架 + 数据模型修复。

**工作项**：
1. 新建 `capabilities/` / `tools/` 目录，空 `__init__.py`
2. 写 `capabilities/base.py`（Capability 基类 + registry）+ `tools/base.py`（Tool 基类）
3. **改 `models/report.py::IncidentReport.claims` 为 `list[Claim]`**
4. 修复 `agents/planner.py` + `agents/report.py` 消费侧
5. 重跑 fixture `python main.py --claims-from tests/fixtures/claims_conspiracy_baseline.json`，验证 `report_raw.json` 带结构化 claims
6. 刷新 `sample_runs/run_fixed_claims_baseline/`

**交付物**：能跑通现有测试，`report_raw.json` 新 schema。

### Phase 1 · 垂直切片 A：TopicOverview + EmotionInsight（5-7 天）

**目标**：第一个端到端 chat 问答。

**工作项**：
1. 实现 3 个 Tool：`list_runs` / `get_run_summary` / `get_topics`（`tools/run_query_tools.py`）
2. 实现 2 个 Capability：`TopicOverviewCapability` / `EmotionInsightCapability`
3. 写 `agents/router.py` 的最小版本（只识别 `topic_overview` + `emotion_analysis` + `other`）
4. 写 `agents/chat_orchestrator.py` + `services/answer_composer.py` + `services/session_store.py`
5. 写 `api/routes/chat.py` 的 `POST /chat/query`
6. 写 `ui/pages/0_Chat.py`（streamlit chat_input + history + session_id 管理）
7. **验收**：UI 能问"今天讨论了什么"，返回前 5 个 topic + 情绪摘要

### Phase 2 · 垂直切片 B：ClaimStatus + PropagationInsight（5-7 天）

**目标**：带证据的事实/谣言判断 + 传播解释。

**工作项**：
1. 实现 4 个 Tool：`get_claims` / `get_claim_details` / `get_primary_claim` / `retrieve_evidence_chunks`
2. 实现 3 个 Tool：`retrieve_official_sources` / `query_topic_graph` / `get_social_metrics`
3. 实现 2 个 Capability：`ClaimStatusCapability` / `PropagationInsightCapability`
4. Router 扩展到 4 个 intent
5. UI chat page 右侧面板接入：evidence 源 + 图谱节选
6. **验收**：UI 能问"Rothschild 有证据吗"、"谁在带动这个主题"，返回结构化判断 + 证据摘要

### Phase 3 · 垂直切片 C：Visual + Compare + 完整编排（5-7 天）

**目标**：对话式出图 + run 对比；Router / Composer 提升到完整版。

**工作项**：
1. 实现 2 个 Tool：`get_intervention_decision` / `generate_visual_card`
2. 实现 2 个 Capability：`VisualSummaryCapability`（抽 planner.py Step 9） / `RunCompareCapability`
3. Router 升级到 6 个 intent + `followup`
4. AnswerComposer 完整化（支持图片/图卡/文本混合输出）
5. UI chat page 支持图卡渲染（`st.image` in chat message）
6. **验收**：对话里出现 Rebuttal Card、Evidence/Context Card、Abstention Block 三种视觉输出分支；能问"这次和上次比变化大吗"

### Phase 4（可选，交付后）

- 真 MCP 协议包装（`mcp` pypi 包 + server entry）
- `POST /runs` 触发后台 precompute 任务
- 会话持久化升级为 SQLite
- 多轮复杂推理（Capability 组合）
- 人工 review / adjudication loop

---

## 14. LOC 估计

| 模块 | 新增 LOC | 改动 LOC |
|---|---|---|
| `capabilities/*.py` (6 个) | ~900 | - |
| `tools/*.py` (5 文件 12 个) | ~500 | - |
| `agents/router.py` | ~150 | - |
| `agents/chat_orchestrator.py` | ~300 | - |
| `services/answer_composer.py` | ~200 | - |
| `services/session_store.py` | ~100 | - |
| `api/routes/chat.py` + `capabilities.py` | ~200 | - |
| `api/routes/runs.py` 扩展 get_topics/get_claims | - | ~80 |
| `ui/pages/0_Chat.py` + `components/chat_response.py` | ~350 | - |
| `models/report.py` 改 `claims` 字段 + `models/session.py` + `models/chat.py` | ~80 | ~30 |
| `agents/planner.py` / `agents/report.py` 消费侧 | - | ~50 |
| 单元测试 `tests/test_capabilities_*.py` + `tests/test_tools_*.py` | ~600 | - |
| **合计** | **~3380** | **~160** |

实际 3 个 Phase 约 **15-21 天**，与原方案估算一致，但**落地确定性更高**（全部垂直切片、Phase 末尾都能跑通）。

---

## 15. 风险与边界

### 15.1 不让 Agent 回到"直接读存储"

Router / ChatOrchestrator **禁止** `import services.chroma_service` / 直接读 `data/runs/*.json`。必须走 Capability → Tool。架构一旦退化，复用性优势全失。

### 15.2 不把 Capability 做成"新 agent 壳子"

每个 Capability 必须有明确 Pydantic Input / Output。**不允许**返回 dict 或 raw LLM string。

### 15.3 Tool 不做业务判断

`retrieve_evidence_chunks` 不判断 stance；`generate_visual_card` 不判断 decision。这些都是 Capability 的职责。

### 15.4 AnswerComposer 不做新推理

AnswerComposer 只负责**把 Capability 输出翻译成人话**，不引入新的 LLM 判断。否则 Capability 的结构化输出失去意义。

### 15.5 不要在首版就做图数据库

内存重建 NetworkX 足够。Phase 4 若遇性能墙再考虑。

### 15.6 Session state 首版不上 DB

JSON 文件 per session_id 足够。单机/单用户场景下并发锁问题不存在。

### 15.7 保留 Precompute Pipeline 的完整性

`agents/planner.py` 不拆分、不重写。chat 路径**不触发** precompute，只查已有 artefact。需要新 run 时走 CLI 或（Phase 4）`POST /runs`。

### 15.8 不要在 Chat 里放超过 3 个 LLM 调用

目前主链路：Router(LLM) → Capability(可选 LLM) → AnswerComposer(LLM) = 2-3 LLM 调用。不加 critic / reviewer / planner layer，否则时延爆炸。

---

## 16. 成功标准（对比原方案 §13）

改造完成后，系统应具备：

### 16.1 用户体验
- chat page 为主入口，Research 页面作为底层查询工具保留
- 用户可自然提问 → 返回结构化回答 + 证据摘要 + 可选图卡
- 支持围绕同一 run / topic 连续追问（session state 生效）

### 16.2 系统能力
- chat 路径不触发 precompute pipeline
- Capability 输出可独立测试、可替换 LLM prompt 不影响 Tool
- 当前 `sample_runs/` 两份样例都能被 chat 查询

### 16.3 答辩展示
- 可清晰陈述"Agent + Capability + Tool + Knowledge Layer 分层架构"
- 可 demo 5 类典型问答：热点、情绪、证据、传播、图卡
- 保留原有 run-centric 研究深度（Run Detail 页面 + report.md）

---

## 17. 结论

本修订版在原方案基础上完成三项关键收敛：

1. **术语清理**：`skills/` → `capabilities/`，`mcp_tools/` → `tools/`，消除与现有目录冲突 + 明确 Tool 是普通 Python 模块
2. **工作量重算**：通过对现有 10 个 agent + 9 个 service + 已有 API + UI 的审计，确认 **70% 的 Capability 和 Tool 是对现有代码的封装**，实际新写 ~3380 LOC
3. **节奏修正**：从"Skills 先做、Tools 后做"的倒序，改为"每个垂直切片 Capability + 对应 Tool 一起落地"，Phase 末尾都能跑通

保留原方案的全部核心理念：分层职责、决策先于视觉、actionability 统一口径、session-centric 交互、内存图即时重建、垂直可复用的 Capability。

一句话总结：

> **Agent 负责决策与编排，Capability 负责任务闭环，Tool 负责底层查询；旧的 Precompute Pipeline 完整保留并退居数据层；不上真 MCP 协议、不上图数据库、不重写现有 agent —— 只做必要的一次数据模型修复（`claims` 字段结构化）和一次分层封装。**

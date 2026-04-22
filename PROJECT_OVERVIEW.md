# Society Analysis — Project Overview

> 社交媒体虚假信息分析 + 交互式问答系统。离线批处理管道（Precompute Pipeline）持续产出可复现的 run artefact；上层会话式 AI Agent 通过 **Agent → Capability → Tool → Service** 四层架构把 run 数据转化为自然语言问答、事实核查、证据检索、传播解释、对比与出图。

本文档是项目当前状态（Phase 0–3 完成后）的单一综合描述。快速开始、启动命令、目录树见 [README.md](README.md)，本文补齐设计、契约、存储、管道细节。

---

## 1. 项目定位

两条互不干扰的路径：

| 路径 | 入口 | 产出 | 角色 |
| --- | --- | --- | --- |
| **Precompute Pipeline** | `python main.py …` → `agents/planner.py`（24 stage） | `data/runs/{run_id}/` 5 件 artefact | 数据层 — 提供"已经算好的分析状态"；种群级统计、聚类、社区、传播指标必须在此批处理产出 |
| **Interactive Agent** | `POST /chat/query` → `agents/chat_orchestrator.py` | session JSON + 自然语言回答 + 可选图卡 | 会话层 — 把 run artefact 按用户问题即时转成可读回答，**不触发新 precompute**，只查已有数据 |

核心判断：
- Precompute 是批处理 — 一次 run 全量跑 24 stage，是因为下游指标（misinfo_risk、community_modularity、bridge_influence_ratio）都是 population-level，必须先聚类/建图才能回答"谁在带动这个主题"。
- Chat 是懒执行 — 用户问到哪一个方面才调哪一个 Capability；同一 run 的 claim/topic 可被反复问而不重跑。

---

## 2. 架构分层

```
            ┌────────────────────────── UI ─────────────────────────┐
            │ Streamlit                                              │
            │   ui/pages/0_Chat.py          ← 默认主入口（chat-first）│
            │   ui/pages/1_Run_List.py      ← run 列表（研究视图）    │
            │   ui/pages/2_Run_Detail.py    ← run 详情（研究视图）    │
            │   ui/components/chat_response.py  (inline per-turn)    │
            │   ui/components/analysis_tabs.py  (right-panel 5 tabs) │
            └──────────────────▲─────────────────────────────────────┘
                               │ HTTP
            ┌──────────────────┴──────────────── API (FastAPI) ─────┐
            │ api/routes/chat.py          POST /chat/query           │
            │ api/routes/runs.py          GET  /runs, /runs/{id}/*   │
            │ api/routes/capabilities.py  POST /capabilities/<name>  │
            │ api/routes/artifacts.py     GET  /artifacts/{run}/...  │
            └──────────────────▲─────────────────────────────────────┘
                               │
        ┌──────────────────────┴──────────────── Agent ─────────────┐
        │ agents/chat_orchestrator.py                                │
        │   → agents/router.py           (intent 分类)                │
        │   → capabilities/*             (领域任务闭环)                │
        │   → services/answer_composer.py (capability → 自然语言)     │
        │   → services/session_store.py   (会话 JSON 持久化)          │
        └──────────────────▲─────────────────────────────────────────┘
                           │
        ┌──────────────────┴──────────────── Capability ────────────┐
        │ capabilities/topic_overview_capability.py                  │
        │ capabilities/emotion_insight_capability.py                 │
        │ capabilities/claim_status_capability.py                    │
        │ capabilities/propagation_insight_capability.py             │
        │ capabilities/visual_summary_capability.py                  │
        │ capabilities/run_compare_capability.py                     │
        │ capabilities/explain_decision_capability.py                │
        └──────────────────▲─────────────────────────────────────────┘
                           │
        ┌──────────────────┴──────────────── Tool (Pydantic) ───────┐
        │ tools/run_query_tools.py    list_runs / get_run_summary /  │
        │                             get_topics / get_claims /       │
        │                             get_claim_details               │
        │ tools/evidence_tools.py     retrieve_evidence_chunks /      │
        │                             retrieve_official_sources       │
        │ tools/graph_tools.py        query_topic_graph /             │
        │                             get_social_metrics /            │
        │                             get_propagation_summary         │
        │ tools/decision_tools.py     get_intervention_decision /     │
        │                             get_counter_effect_history      │
        │ tools/visual_tools.py       generate_clarification_card /   │
        │                             generate_evidence_context_card  │
        └──────────────────▲─────────────────────────────────────────┘
                           │
        ┌──────────────────┴──────────────── Services / Storage ────┐
        │ ChromaService · KuzuService · PostgresService · SQLite     │
        │ EmbeddingsService · StableDiffusionService                 │
        │ WikipediaService · NewsSearchService · CounterEffectService│
        │ data/runs/ · data/sessions/ · data/chroma/ · data/kuzu_graph│
        └────────────────────────────────────────────────────────────┘
                           │
        ┌──────────────────┴──────────────── Precompute (批) ────────┐
        │ agents/planner.py              24 stage 主干                │
        │ agents/{ingestion, knowledge, analysis, community, risk,   │
        │         visual, report, counter_message, critic}           │
        │ 触发：CLI (main.py) / fixture / 定时任务                    │
        │ Chat 路径**不会**触发这里                                   │
        └────────────────────────────────────────────────────────────┘
```

### 分层契约（强约束）

1. **Router / ChatOrchestrator** 不得 `import services.chroma_service`，也不得读 `data/runs/*.json` —— 全部走 Capability → Tool。
2. **Capability** 必须返回 Pydantic Output 对象，不返回 dict / raw LLM string。
3. **Tool** 不做业务判断（`retrieve_evidence_chunks` 不判断 stance；`generate_visual_card` 不判断 decision）。
4. **AnswerComposer** 只把 Capability 输出翻译成自然语言，不引入新的 LLM 推理。
5. **Chat 主链路 LLM 调用 ≤ 3**：Router + 可选 Capability LLM + AnswerComposer。
6. **Precompute Pipeline 完整保留**：`agents/planner.py` 一行未动。

---

## 3. Precompute Pipeline（24 stage 概览）

入口 `agents/precompute_pipeline.py::PrecomputePipeline.run()`。一次 run 的完整阶段（condensed）：

| # | Stage | 关键输出 |
|---|---|---|
| 1 | fetch_posts | Reddit / Telegram / fixture JSON → 原始 post 流 |
| 2 | ingest | `PostgresService` 写入（不可用时降级 SQLite）；`ChromaService` 向量化 |
| 3 | normalize | 统一 schema（post_id, author, text, ts, subreddit/chat） |
| 4 | emotion_baseline | 每条 post 基础情绪打分 |
| 5 | claim_extract | 抽取候选 claim + 归一化 + 去重（`unique_claims`） |
| 6 | actionability | 判定 `claim_actionability ∈ {actionable, non_actionable, insufficient_evidence}` + `non_actionable_reason` |
| 7 | evidence_gather | 按 claim 向 Chroma 查 evidence_chunks；Wikipedia + NewsSearch 补官方源 |
| 8 | stance_score | evidence 对 claim 的立场：`supporting / contradicting / uncertain` |
| 9 | tier_classify | evidence tier：`official / reputable_media / user_generated / low_credibility` |
| 10 | claim_verdict | 5 档裁决：`supported / contradicted / disputed / insufficient / non_factual` |
| 11 | topic_cluster | 对全量 post 做嵌入聚类 → `TopicSummary[]` |
| 12 | topic_rollup | 每 topic 的 `post_count / velocity / dominant_emotion / emotion_distribution / misinfo_risk` |
| 13 | community_detect | Louvain / Leiden 社区检测 → `CommunityAnalysis.communities` |
| 14 | account_roles | 账户分类（`originator / amplifier / bridge / commentator / ...`） |
| 15 | bridge_influence | `bridge_influence_ratio` + `role_risk_correlation` |
| 16 | propagation_summary | `PropagationSummary`：协同对、速度、跨社区桥 |
| 17 | coordinated_detect | 时间窗口内的可疑同步发帖 |
| 18 | intervention_decide | `InterventionDecision`：`rebut / clarify / abstain` + `decision_reason` |
| 19 | counter_message | 若决定干预 — 生成反制文案（`agents/counter_message.py`） |
| 20 | critic_review | 对反制文案的自我审查 |
| 21 | visual_cards | 分支：rebut → Clarification Card PNG；clarify → Evidence Context Card PNG；abstain → 结构化 abstention_block 文本 |
| 22 | metrics_aggregate | `services/metrics_service.py` 汇总 → `metrics.json` |
| 23 | report_render | `agents/report.py` → `report.md` + `report_raw.json` |
| 24 | manifest_write | `services/manifest_service.py` → `run_manifest.json`（schema_version / inputs / outputs hash / timestamps） |

### Run artefact（`data/runs/{run_id}/`）

| 文件 | 内容 |
|---|---|
| `run_manifest.json` | schema 版本、输入、产物哈希、阶段时间戳 |
| `report.md` | 面向人读的 Markdown 报告 |
| `report_raw.json` | `IncidentReport` Pydantic 序列化 — chat 层的核心数据源；含 `claims: list[Claim]`（结构化）、`topic_summaries`、`community_analysis`、`propagation_summary`、`intervention_decision`、`counter_message`、`critic_review` |
| `metrics.json` | 聚合指标：`misinfo_risk` / `bridge_influence_ratio` / `role_risk_correlation` / `community_modularity_q` / `account_role_counts` / `claim_verdict_counts` |
| `counter_visuals/` | PNG 图卡（Clarification / Evidence Context Cards） |

### 数据契约要点

- `IncidentReport.claims: list[Claim]`（Phase 0 完成的数据模型升级）—— 每个 Claim 带 `claim_id / normalized_text / claim_actionability / non_actionable_reason / supporting_evidence / contradicting_evidence / evidence_tiers / evidence_summary()`。旧的 `list[str]` 已全量迁移。
- `TopicSummary` 含 `dominant_emotion` + `emotion_distribution` + `representative_claims`，EmotionInsightCapability 直接复用。
- `PropagationSummary` 含 `bridge_accounts / coordinated_pairs / account_role_counts`，PropagationInsightCapability 直接复用。

---

## 4. Storage Layer

| 存储 | 用途 | 路径 / 连接 |
|---|---|---|
| **Filesystem** | run artefact、session、运行时日志 | `data/runs/` · `data/sessions/` · `sample_runs/` |
| **Chroma** | 2 个 collection：`articles`（官方新闻 / Wikipedia / NewsSearch 的 chunks — evidence RAG 查的就是它）、`claims`（归一化 claim 文本，用于 claim 去重） | `data/chroma/`（本地持久化）|
| **Kuzu** | 图数据（社区结构快照，可选） | `data/kuzu_graph/` |
| **PostgreSQL** | 原始 post 落盘（可选；不可用时降级 SQLite） | `.env::POSTGRES_*` |
| **SQLite** | Postgres 不可用时的 fallback；counter_effect 历史 | `data/app.sqlite` |
| **内存 NetworkX** | Chat 层按需重建的社交图（`@lru_cache(maxsize=8)`），规避图数据库部署 | `tools/graph_tools.py::query_topic_graph` |

首版 session state 为 JSON 文件（`data/sessions/{session_id}.json`），无并发锁 —— 单用户单机场景足够。

---

## 5. Capability × Tool 矩阵

### 7 个 Capability

| Intent | Capability | 典型问题 | 调用的 Tool |
|---|---|---|---|
| `topic_overview` | `TopicOverviewCapability` | 今天讨论了什么 / what's trending | `list_runs` + `get_topics` |
| `emotion_analysis` | `EmotionInsightCapability` | 情绪怎么样 / 哪个话题最愤怒 | `get_topics` |
| `claim_status` | `ClaimStatusCapability` | X 这个说法有证据吗 / fact check | `get_claims` + `get_claim_details` + `retrieve_evidence_chunks` + `get_intervention_decision` |
| `propagation_analysis` | `PropagationInsightCapability` | 谁在带动这个主题 / 是否协同 | `query_topic_graph` + `get_social_metrics` |
| `visual_summary` | `VisualSummaryCapability` | 用一张图总结 / 出张反驳卡 | `get_primary_claim` + `get_claim_details` + `retrieve_evidence_chunks` + `get_intervention_decision` + `generate_clarification_card` / `generate_evidence_context_card` |
| `run_compare` | `RunCompareCapability` | 这次和上次比变化大吗 | `list_runs` + `get_run_summary` + `get_social_metrics` |
| `explain_decision` | `ExplainDecisionCapability` | 为什么要（不要）反制 | ClaimStatus + `get_intervention_decision` + 可选 visual |

每个 Capability 的 **Input / Output 都是 Pydantic BaseModel**，可独立单元测试。LLM 调用只出现在需要自然语言合成的环节（`interpretation` / `verdict_summary` / `narrative`），`temperature ≤ 0.3`。

### 12 个 Tool

分 5 个文件：

- **`tools/run_query_tools.py`** — `list_runs` / `get_run_summary` / `get_topics` / `get_claims` / `get_claim_details` / `get_primary_claim`
- **`tools/evidence_tools.py`** — `retrieve_evidence_chunks`（封装 `ChromaService.query_articles`）/ `retrieve_official_sources`（封装 Wikipedia + NewsSearch）
- **`tools/graph_tools.py`** — `query_topic_graph`（内存 NetworkX，从 `report_raw.json::community_analysis + propagation_summary` 按需重建，`lru_cache(maxsize=8)`）/ `get_social_metrics` / `get_propagation_summary`
- **`tools/decision_tools.py`** — `get_intervention_decision` / `get_counter_effect_history`
- **`tools/visual_tools.py`** — thin wrapper 调 `agents/visual.py::generate_clarification_card` / `generate_evidence_context_card`

---

## 6. Chat Orchestrator 主链路

```
POST /chat/query {session_id, message}
  ↓
session = SessionStore.load(session_id)
  ↓
router_out = Router.classify(message, session_context)
   # → {intent, targets={run_id?, topic_id?, claim_id?}, confidence}
  ↓
capability = CAPABILITY_REGISTRY[intent]
cap_output = capability.run(CapabilityInput(**targets))
  # → Pydantic Output（CapabilityInput 从 session 继承 current_* 做 followup）
  ↓
answer_text = AnswerComposer.compose(message, cap_output, session_context)
  ↓
SessionStore.append_turn(user_msg, assistant_reply, capability_used)
  ↓
return ChatResponse(answer_text, capability_used, capability_output, visual_paths)
```

### Router 意图分类

8 个 intent：`topic_overview` · `emotion_analysis` · `claim_status` · `propagation_analysis` · `visual_summary` · `run_compare` · `explain_decision` · `followup`（读 session state，延用上一 capability）。输出：

```python
class RouterOutput(BaseModel):
    intent: Literal[...]
    targets: RouterTargets  # run_id / topic_id / claim_id 全可选
    confidence: float
    fallback_reason: Optional[str]
```

LLM 模式 `response_format={"type": "json_object"}`，默认 `OPENAI_MODEL=gpt-4o`，`temperature=0`。

### Session State（`data/sessions/{session_id}.json`）

```json
{
  "session_id": "s-abc123",
  "created_at": "2026-04-22T10:00:00Z",
  "current_run_id": "20260421-231801-6afd7c",
  "current_topic_id": null,
  "current_claim_id": "c1",
  "recent_visuals": ["data/runs/.../counter_visuals/c1_clarification.png"],
  "conversation": [
    {"role": "user", "content": "what's trending?", "capability_used": null, "at": "..."},
    {"role": "assistant", "content": "...", "capability_used": "topic_overview", "at": "..."}
  ]
}
```

`current_*` 字段让后续问句（"那这个话题的情绪呢"）无需重新指定目标 —— Router 检测到 followup 意图时直接从 session 继承。

---

## 7. UI — 三栏 Chat Workspace + 5-tab 分析面板

`ui/pages/0_Chat.py` 是默认主入口，布局：

```
┌──────────────┬────────────────────────────┬────────────────────────┐
│ st.sidebar   │   Conversation (chat_col)  │  Analysis (analysis_col)│
│              │                            │                         │
│ - session id │ - st.chat_message stream   │ st.tabs([Evidence,      │
│ - breadcrumb │ - Structured output        │           Topic,        │
│   current_*  │   expander (per-turn       │           Graph,        │
│ - New session│   render_capability_output)│           Metrics,      │
│ - prompt     │ - st.chat_input            │           Visual])      │
│   chips      │                            │                         │
│ - API health │                            │ 每次 capability 回答    │
│              │                            │ 通过 route_capability_  │
│              │                            │ to_panels() 写入         │
│              │                            │ st.session_state[       │
│              │                            │   "panel_<key>"]        │
└──────────────┴────────────────────────────┴────────────────────────┘
```

实现：`st.columns([3, 2], gap="large")` 分 Conversation + Analysis 两栏，`st.sidebar` 负责 session 控制。

### 右侧 5 个 Tab 与 Capability 映射

`ui/components/analysis_tabs.py::route_capability_to_panels(cap_name, output)`：

| Capability | Evidence | Topic | Graph | Metrics | Visual |
|---|:---:|:---:|:---:|:---:|:---:|
| `topic_overview` | | ✓ | | | |
| `emotion_analysis` | | ✓ | | | |
| `claim_status` | ✓ | ✓ | | | |
| `propagation_analysis` | | | ✓ | ✓ | |
| `visual_summary` | | | | | ✓ |
| `run_compare` | | | | ✓ | |
| `explain_decision` | | ✓ | | | ✓（可选）|

空状态给出引导问句；Tab 内有 badge / color coding：
- **Verdict badges**：`supported` · `contradicted` · `disputed` · `insufficient` · `non_factual`（5 档，禁用"绝对真/假"）
- **Visual status**：`rendered` / `abstained` / `no_decision` / `render_failed` / `insufficient_data`
- **Run-compare arrows**：`↑ up` / `↓ down` / `→ flat` / `· unknown`

### 辅助页面（Research 视图）

- `ui/pages/1_Run_List.py` — run 目录浏览，列出 `data/runs/` + `sample_runs/`
- `ui/pages/2_Run_Detail.py` — 单 run 深挖，嵌入 `metric_cards` / `community_graph` / `emotion_chart` 组件

---

## 8. API 端点

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/chat/query` | 主对话入口 |
| GET | `/chat/session/{id}` | 回读 session（UI breadcrumb 用） |
| GET | `/runs` | 列 runs（data + sample，带 `source` 字段） |
| GET | `/runs/{run_id}` | 单 run 摘要 |
| GET | `/runs/{run_id}/report` | `report_raw.json` |
| GET | `/runs/{run_id}/metrics` | `metrics.json` |
| GET | `/artifacts/{run_id}/{path}` | PNG / 其他 artefact 透传 |
| POST | `/capabilities/{name}` | 绕过 Router 直调某 Capability（调试 / 外部集成） |
| GET | `/health` | 健康 + `runs_root` |

CORS 白名单：`http://127.0.0.1:8501` · `http://localhost:8501`。

---

## 9. CLI 模式

`main.py` 支持以下模式，均产出 `data/runs/{timestamp}-{hash}/`：

```bash
# 从 Reddit 抓取
python main.py --subreddit conspiracy --days 3

# 从 Telegram 抓取（需 .env 配置）
python main.py --telegram-channel @somechannel --days 1

# 从 fixture JSON 跑（无需网络；用于测试）
python main.py --claims-from tests/fixtures/claims_conspiracy_baseline.json

# watch 模式 — 周期性重抓
python main.py --subreddit conspiracy --watch --interval 3600
```

Chat 路径**不会**调用 `main.py` —— 它只读 `data/runs/` + `sample_runs/` 里已有的 artefact。

### 9.1 Scheduler（`scripts/scheduler.py`）

`scripts/scheduler.py` 把 precompute pipeline 包成可定期调度的任务；任务定义在 `scripts/scheduler_tasks.yaml`（示例见 `scheduler_tasks.example.yaml`）。

```bash
# 长驻进程模式（APScheduler BlockingScheduler）
python scripts/scheduler.py --mode apscheduler

# 一次性运行单个任务（配合 cron / GitHub Actions cron / Windows Task Scheduler）
python scripts/scheduler.py --mode once --task fixture_smoke

# 列出当前加载到的所有任务
python scripts/scheduler.py --mode list
```

每个任务 trigger 可以是 `cron: "0 */6 * * *"` 或 `interval_seconds: 21600`；源可以是 `channel` / `subreddit` / `reddit_query` / `claims_from`。Scheduler 调的仍是 `build_precompute_pipeline()` + `ManifestService.new_run()`，和 `main.py` 同一条代码路径，**不**穿透到 chat orchestrator。

---

## 10. 测试

```bash
pytest tests/test_phase1_chat.py tests/test_phase2_chat.py tests/test_phase3_chat.py -v
# 37 passed
```

- **Tools** 针对临时 `data/runs/` 目录跑，不碰真实 Chroma / Stable Diffusion
- **LLM 调用**（Router / Composer / Emotion interpretation）在测试里全部 patch 掉
- **Visual** 渲染 patch `VisualAgent`，不触发 Stable Diffusion
- 固定 fixture：`tests/fixtures/claims_conspiracy_baseline.json`
- 样例 run：`sample_runs/run_fixed_claims_baseline/` 可直接被 chat 查询（run 路由 fallback 到 sample_runs）

---

## 11. 设计约束（Hard Rules）

1. **Kuzu 是唯一 canonical 图后端** — 在线图查询（`tools/graph_tools.py`）走 Kuzu Cypher；NetworkX 仅作 debug / 本地可视化 fallback。
2. **Chat 不触发 precompute** — `agents/chat_orchestrator.py` 禁止调 `PrecomputePipeline`；chat 只读 artefact + 按需 Tool 查询。
3. **Claim 裁决统一 5 档** — `supported / contradicted / disputed / insufficient / non_factual`；禁用"绝对真/假"语言。
4. **Actionability 三档** — `actionable / non_actionable / insufficient_evidence`；`non_actionable` 必须带 `non_actionable_reason`。
5. **Intervention Decision 先于 Visual** — VisualSummaryCapability 规则：`decision == rebut` → Clarification Card；`decision == clarify` → Evidence Context Card；`decision == abstain` → 结构化 `abstention_block` 文本，**不**硬出 PNG。
6. **Tool 不做业务判断** — evidence retrieval 不裁决 stance；visual 不裁决 decision；graph 查询不做摘要。
7. **Capability 返回 Pydantic Output** — 禁止 dict / raw LLM string。
8. **Router / Planner / Orchestrator 不 import services** — 必须走 Capability → Tool。
9. **Chat 主链路 LLM ≤ 3 次**（单步模式）— Router + 可选 Capability + AnswerComposer。Planner DAG 模式每个 workflow template 硬上限 5 次。
10. **Session state 首版 JSON 文件** — 无并发锁（单用户单机）。
11. **命名诚实**：`subagents` / executable 组件放在 `capabilities/` 与 `tools/`；`skills/` 只放 Claude Code markdown skill pack（文档用途）。MCP 协议为未来可选路径，当前阶段是 internal tool registry。
12. **批处理 vs 按需的边界**（§15）是硬契约：跨越会破坏延迟目标。

---

## 12. 模块边界速查

| 模块 | 职责 | 由谁调用 |
|---|---|---|
| `agents/precompute_pipeline.py` | 24 stage 离线 batch 主干 | CLI `main.py` · Scheduler |
| `agents/planner.py` | 在线 query Planner（bounded DAG） | `chat_orchestrator.py` |
| `agents/{ingestion, knowledge, analysis, community, risk, visual, report, counter_message, critic}` | Precompute 的各 stage 执行者 | 只被 `precompute_pipeline.py` 调 |
| `agents/router.py` | 意图分类（Chat 链路第 1 次 LLM） | `chat_orchestrator.py` |
| `agents/chat_orchestrator.py` | Chat 入口、session 管理、Planner 调度、答案组合 | `api/routes/chat.py` |
| `capabilities/*.py` | 领域任务闭环，Pydantic I/O；带 manifest 供 Planner 检索 | `chat_orchestrator.py` · `agents/planner.py` · `api/routes/capabilities.py` |
| `tools/*.py` | 原子查询 / 生成（Kuzu Cypher、RAG 检索、SD 出图等） | 只被 `capabilities/*` 调 |
| `services/chroma_service.py` · `kuzu_service.py` · `postgres_service.py` | 存储层 | Precompute agent + Tool 层 |
| `services/session_store.py` | session JSON 读写 | `chat_orchestrator.py` 唯一调用方 |
| `services/answer_composer.py` | Capability → 自然语言（Chat 链路最后一次 LLM） | `chat_orchestrator.py` 唯一调用方 |
| `services/manifest_service.py` | Capability manifest 注册 / 查询 | `agents/planner.py` |
| `models/claim.py` · `report.py` · `community.py` | Pydantic 数据契约 | 全局共享 |
| `models/session.py` · `chat.py` · `manifest.py` | 会话 + manifest 契约 | API + Orchestrator + Planner |
| `skills/*/SKILL.md` | Claude Code markdown skill pack（文档用途；**不是**可执行 Python） | 不被 Python 代码引用 |

---

## 13. 历史说明 + 当前改造

项目经历了以下主要阶段：

- **Phase 0（早期）** — 目录打底 + `IncidentReport.claims: list[str] → list[Claim]` 数据模型升级
- **Phase 1（早期）** — 垂直切片 A：`TopicOverview` + `EmotionInsight` + Chat 骨架
- **Phase 2（早期）** — 垂直切片 B：`ClaimStatus` + `PropagationInsight` + 证据 / 图谱 Tool
- **Phase 3（早期）** — 垂直切片 C：`VisualSummary` + `RunCompare` + 完整 Router / Composer + 三栏 UI

**当前改造**（按 `complete_project_transformation_plan.md`）：

- **Phase 0-新** — 结构性清理：`agents/planner.py → agents/precompute_pipeline.py` 重命名；定义 Router/Planner 分工；命名约束；MCP 不在 scope
- **Phase 1-新** — 在线编排核心：新建 `agents/planner.py`（bounded DAG Planner）；Capability manifest schema；answer-composition contract
- **Phase 2-新** — 数据/查询后端对齐：`tools/graph_tools.py` 重写为 Kuzu Cypher；on-demand claim verdict；官方 + 社区证据对比
- **Phase 3-新** — 定时刷新 + 缓存：APScheduler / cron job；run-selection 策略
- **Phase 4-新** — UI 交互层完善：workflow 过程面板、证据面板、图面板

### 15. Batch vs On-demand 契约

| 类型 | 放在哪里 | 何时跑 |
|---|---|---|
| 社区抓取 / 清洗 / 去重 | precompute | scheduler（日更）或手动 CLI |
| Claim 抽取 + 归一化 + 映射 | precompute | 同上 |
| Topic 聚类（LLM） | precompute | 同上 |
| 社区检测 / role 分类 | precompute | 同上 |
| Kuzu 图边写入 | precompute | 同上 |
| Embedding 刷新（articles + posts） | precompute | 同上 |
| 基线 run metrics / report artefact | precompute | 同上 |
| Topic summary LLM 重写 | **on-demand** | chat query |
| Claim verdict 解释 + 证据检索 | **on-demand** | chat query |
| 官方 vs 社区对比 | **on-demand** | chat query |
| 传播解释（根据 Kuzu 子图） | **on-demand** | chat query |
| VisualSummary（Clarification Card / Evidence Card） | **on-demand** | chat query |
| Answer 合成 | **on-demand** | chat query |

### 16. Hybrid Claim-Verdict 策略

- **Batch 侧**：candidate claims · normalized claims · topic↔claim 映射 · post↔claim 映射 · 缓存的候选证据（可选）
- **On-demand 侧**：检索 official evidence + 社区 post chunks · support/contradict/insufficient 判断 · 生成可解释的 verdict wording + citation

### 17. 命名约束（防止未来混乱）

- `skills/` = Claude Code markdown skill pack（纯文档，**不**执行）
- `capabilities/` = 可执行 Python subagent 闭环（Pydantic I/O）
- `tools/` = 原子 Python 查询 / 生成
- `subagents/` / `agent_tools/` 命名在本项目**不使用**（历史遗留术语请勿引入）
- **MCP** 当前阶段 = internal tool registry，不是 protocol-level server；避免"MCP-ready"等过度声称

原始设计文档（`interactive_agent_transformation_plan_skills_mcp.md` · `ui_design_plan.md`）已合并，新改造方案见根目录 `complete_project_transformation_plan.md`。

---

## 14. 关键文件索引

| 主题 | 文件 |
|---|---|
| Chat 入口 | `agents/chat_orchestrator.py` · `agents/router.py` · `services/answer_composer.py` · `services/session_store.py` |
| Capability | `capabilities/{topic_overview,emotion_insight,claim_status,propagation_insight,visual_summary,run_compare,explain_decision}_capability.py` · `capabilities/base.py` |
| Tool | `tools/{run_query,evidence,graph,decision,visual}_tools.py` · `tools/base.py` |
| Precompute | `agents/precompute_pipeline.py`（24 stage 主干）· `main.py`（CLI） · Scheduler |
| Online Planner | `agents/planner.py`（bounded DAG workflow planner） · `services/manifest_service.py` |
| 数据契约 | `models/{report,claim,community,session,chat}.py` |
| API | `api/app.py` · `api/routes/{chat,runs,capabilities,artifacts}.py` |
| UI | `ui/pages/{0_Chat,1_Run_List,2_Run_Detail}.py` · `ui/components/{chat_response,analysis_tabs,metric_cards,community_graph,emotion_chart}.py` |
| 测试 | `tests/test_phase{1,2,3}_chat.py` · `tests/test_tools_*.py` · `tests/test_capabilities_*.py` · `tests/fixtures/` |
| 样例 run | `sample_runs/run_fixed_claims_baseline/` · `sample_runs/run_live_demo/` |

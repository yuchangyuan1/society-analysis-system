# Society Analysis — Interactive Agent

社交媒体虚假信息分析 + 交互式问答系统。离线批处理管道持续产出 run artefact；上层会话式 AI Agent 通过 **Capability → Tool** 两层架构把 run 数据转化为自然语言问答、事实核查、证据检索、对比与出图。

## 核心能力

两条互不干扰的路径：

1. **Precompute Pipeline**（`agents/planner.py`，24 stage）—— 每次 `python main.py …` 生成一个可复现的 `data/runs/{run_id}/` 目录：
   - `run_manifest.json`、`report.md`、`report_raw.json`、`metrics.json`、`counter_visuals/`
2. **Interactive Agent**（Chat Orchestrator）—— `POST /chat/query` 把用户问题路由到以下 7 个 Capability：

| Intent | Capability | 典型问题 |
| --- | --- | --- |
| `topic_overview` | `capabilities/topic_overview_capability.py` | 今天讨论了什么 / what's trending |
| `emotion_analysis` | `capabilities/emotion_insight_capability.py` | 情绪怎么样 / 哪个话题最愤怒 |
| `claim_status` | `capabilities/claim_status_capability.py` | X 这个说法有证据吗 / fact check |
| `propagation_analysis` | `capabilities/propagation_insight_capability.py` | 谁在带动这个主题 / 是否协同 |
| `visual_summary` | `capabilities/visual_summary_capability.py` | 用一张图总结 / 出张反驳卡 |
| `run_compare` | `capabilities/run_compare_capability.py` | 这次和上次比变化大吗 |
| `explain_decision` | `capabilities/explain_decision_capability.py` | 为什么要（不要）反制 |

## 架构分层

```
            ┌────────────────────────── UI ─────────────────────────┐
            │ Streamlit (ui/pages/0_Chat.py, 1_Run_List, 2_Run_Detail)
            │   ui/components/chat_response.py  (inline turn renderer)
            │   ui/components/analysis_tabs.py  (right-panel 5 tabs)
            └──────────────────▲─────────────────────────────────────┘
                               │ HTTP
            ┌──────────────────┴──────────────── API (FastAPI) ─────┐
            │ api/routes/{chat, runs, capabilities}                  │
            └──────────────────▲─────────────────────────────────────┘
                               │
        ┌──────────────────────┴──────────────── Agent ─────────────┐
        │ agents/chat_orchestrator.py                                │
        │   → agents/router.py           (intent classification)     │
        │   → capabilities/*             (domain task closures)      │
        │   → services/answer_composer.py (capability → 自然语言)   │
        │   → services/session_store.py   (session JSON 持久化)     │
        └──────────────────▲─────────────────────────────────────────┘
                           │
        ┌──────────────────┴──────────────── Tool (Pydantic) ───────┐
        │ tools/run_query_tools.py   list_runs / get_topics /         │
        │                            get_claims / get_claim_details  │
        │ tools/evidence_tools.py    retrieve_evidence_chunks /       │
        │                            retrieve_official_sources       │
        │ tools/graph_tools.py       query_topic_graph /              │
        │                            get_social_metrics /             │
        │                            get_propagation_summary         │
        │ tools/decision_tools.py    get_intervention_decision /      │
        │                            get_counter_effect_history      │
        │ tools/visual_tools.py      generate_clarification_card /    │
        │                            generate_evidence_context_card  │
        └──────────────────▲─────────────────────────────────────────┘
                           │
        ┌──────────────────┴──────────────── Services / Storage ────┐
        │ ChromaService · KuzuService · PostgresService              │
        │ EmbeddingsService · StableDiffusionService                 │
        │ WikipediaService · NewsSearchService · CounterEffectService│
        │ data/runs/ · data/sessions/ · data/chroma/ · data/kuzu_graph│
        └────────────────────────────────────────────────────────────┘
```

**分层契约**：
- Router / ChatOrchestrator 不得直接 `import services.chroma_service`，也不得读 `data/runs/*.json` ——全部走 Capability → Tool。
- Capability 必须返回 Pydantic Output，不返回 dict / raw LLM string。
- Tool 不做业务判断。
- AnswerComposer 不引入新推理，只把 Capability 输出翻译成自然语言。
- Chat 主链路 LLM 调用 ≤ 3（Router + 可选 Capability + AnswerComposer）。

## 目录结构

```
society-analysis-project-update/
├── main.py                     入口（CLI → PrecomputePipeline）
├── config.py                   全局配置（.env 加载）
│
├── agents/                     业务编排层
│   ├── precompute_pipeline.py  ★ 24-stage offline batch pipeline
│   ├── planner.py              ★ online query planner (bounded DAG)
│   ├── chat_orchestrator.py    ★ session-centric chat 入口
│   ├── router.py               ★ intent 分类器（6 intents + followup）
│   ├── ingestion / knowledge / analysis / risk / counter_message
│   ├── critic / visual / report / community
│
├── capabilities/               7 个领域任务闭环（每个 = 1 个 intent）
├── tools/                      12 个原子操作（Pydantic Input/Output）
│
├── services/                   基础设施 + 无状态服务
│   ├── session_store.py        会话 JSON 持久化（data/sessions/）
│   ├── answer_composer.py      Capability → 自然语言
│   ├── chroma_service / kuzu_service / postgres_service
│   ├── embeddings / stable_diffusion / wikipedia / news_search
│   ├── counter_effect / metrics / manifest / monitor
│
├── models/                     Pydantic 数据契约
│   ├── report.py               IncidentReport / InterventionDecision
│   ├── claim.py                Claim / ClaimEvidence
│   ├── community.py            CommunityAnalysis
│   ├── session.py              SessionState / ConversationTurn
│   ├── chat.py                 ChatQuery / ChatResponse
│
├── api/                        FastAPI
│   └── routes/{chat, runs, capabilities}.py
├── ui/                         Streamlit
│   ├── streamlit_app.py
│   ├── pages/0_Chat.py · 1_Run_List.py · 2_Run_Detail.py
│   └── components/
│       ├── chat_response.py    inline per-turn capability renderer
│       └── analysis_tabs.py    right-panel 5 tabs + panel routing
│
├── tests/                      pytest — 37 chat 测试（phase1/2/3）
├── sample_runs/                打包的示例 artefact
├── data/                       运行时产出（runs / sessions / chroma / kuzu）
├── docs/                       架构、脚本、阶段总结
└── skills/                     18 个 Claude Code 风格 skill pack（文档用途）
```

## 快速开始

### 1. 环境

- Python ≥ 3.10（建议 3.11）
- 依赖见 `pyproject.toml`

```bash
pip install -e .
```

### 2. 配置

拷贝 `.env.example`（若无则新建）到 `.env`，至少配置：

```
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o
# 可选：REDDIT / TELEGRAM / X API、STABLE_DIFFUSION
```

### 3. 跑一次 precompute pipeline（可选）

```bash
python main.py --claims-from tests/fixtures/claims_conspiracy_baseline.json
```

产物写入 `data/runs/{timestamp}-{hash}/`。

### 4. 启动交互式 Chat

```bash
# Terminal 1 — API
uvicorn api.app:app --reload --port 8000

# Terminal 2 — UI
streamlit run ui/streamlit_app.py
```

打开浏览器默认到 `http://localhost:8501`，点侧栏 **Chat** 页即可开始问答。

**Chat 页布局**（`ui_design_plan.md` §11 MVP）：

- **左侧 `st.sidebar`** — session id · current run/topic/claim 面包屑 · 新会话按钮 · 常用 prompt 按钮 · API 健康指示
- **中栏 Conversation** — 聊天消息流 + `st.chat_input`，每条 assistant 回复下有 `Structured output` expander 可展开原始 capability 输出
- **右栏 Analysis workspace** — 5 个 tab（Evidence / Topic / Graph / Metrics / Visual），每次 capability 回答会按 `route_capability_to_panels()` 的映射写进对应面板；空状态给出引导问句

API 也可直接调用：

```bash
curl -X POST http://127.0.0.1:8000/chat/query \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"s-demo","message":"what topics are trending?"}'
```

### 5. 不跑新 run，直接用 sample_runs

Run 路由会先查 `data/runs/`，再 fallback 到 `sample_runs/`。所以即便从未跑过 pipeline，也能在 Chat 页直接问 `sample_runs/` 里的示例 run（例如 `run_fixed_claims_baseline`）。

## 测试

```bash
pytest tests/test_phase1_chat.py tests/test_phase2_chat.py tests/test_phase3_chat.py -v
# 37 passed
```

- Tools 针对临时 `data/runs/` 树运行，不碰真实 Chroma / SD。
- LLM 调用（Router / Composer / Emotion interpretation）在测试里全部 patch 掉。
- Visual 渲染 patch `VisualAgent`，不触发 Stable Diffusion。

## 会话状态

首版使用 JSON 文件，路径 `data/sessions/{session_id}.json`，无并发锁。结构：

```json
{
  "session_id": "s-abc123",
  "current_run_id": "20260421-010000-aaaaaa",
  "current_topic_id": null,
  "current_claim_id": "c1",
  "recent_visuals": [],
  "conversation": [
    {"role": "user", "content": "what's trending?", "capability_used": null},
    {"role": "assistant", "content": "...", "capability_used": "topic_overview"}
  ]
}
```

`current_*` 字段使后续问句（"那这个话题的情绪呢"）无需再次指定目标。

## 设计约束（来自 `interactive_agent_transformation_plan_skills_mcp.md`）

- **不上图数据库**：NetworkX 按需重建 + `functools.lru_cache(maxsize=8)`。
- **不触发 precompute**：Chat 只读 run artefact，不调 `PrecomputePipeline`。
- **绝对真/假 禁用**：`ClaimStatusCapability` 只出 5 档 verdict（`supported / contradicted / disputed / insufficient / non_factual`）。
- **Precompute Pipeline 完整保留**：`agents/planner.py` 一行未动，交互化改造只是在其输出上加一层。

## 相关文档

- `interactive_agent_transformation_plan_skills_mcp.md` — 交互化改造蓝图（6 capability × 12 tool，阶段拆分）
- `ui_design_plan.md` — Chat 页三栏布局方案（右侧 5 tab、组件清单、MVP 范围）
- `PROJECT_OVERVIEW.md` — Precompute pipeline 内部 24 stage 详解
- `docs/architecture.md` — 更深入的架构说明
- `docs/demo_script.md` — 演示脚本

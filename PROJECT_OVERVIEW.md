# 社交媒体虚假信息分析系统 — 项目总览

> 文件路径：`society-analysis-project-update/`
> 最后更新：2026-04-19（P0 全部完成 + 研究型 UI 上线）

---

## 一、项目目标

从社交媒体（Reddit / Telegram / X）采集帖子，自动完成：

1. **识别虚假信息声明** — 提取、去重、聚类
2. **评估传播风险** — 速度、异常检测、账号角色
3. **检索权威证据** — 三级证据召回（内部 Chroma → Wikipedia → News）
4. **生成反制内容** — 反驳文字 + 可视化图片卡片（有证据门控）
5. **追踪反制效果** — 跨次运行的闭环对比（部署前后传播速度）

**每次 run 产出可复现、可审计的 artifact 目录** `data/runs/{run_id}/`：
- `run_manifest.json` — 输入参数、模型版本、阈值、git sha、posts 快照哈希
- `report.md` — 模板渲染的 Markdown 报告（LLM 仅包装摘要段落）
- `report_raw.json` — 完整 IncidentReport 结构化对象
- `metrics.json` — 量化指标（evidence_coverage、modularity、closed_loop_rate 等）
- `counter_visuals/` — 本次 run 生成的 PNG 图片卡片

---

## 二、目录结构

```
society-analysis-project-update/
│
├── main.py                  # 入口：解析 CLI 参数，组装服务，调用 PlannerAgent
├── config.py                # 全局配置（从 .env 加载）；含 RUNS_DIR
│
├── agents/                  # 业务逻辑层（每个 Agent 负责一个阶段）
│   ├── planner.py           # ★ 核心：全流程编排，24 个 stage
│   ├── ingestion.py         # 数据采集
│   ├── knowledge.py         # 声明提取、去重、三级证据检索
│   ├── analysis.py          # 传播分析、话题分析、级联预测、免疫策略
│   ├── risk.py              # 风险评估
│   ├── counter_message.py   # 生成反制消息
│   ├── critic.py            # 审核反制消息质量
│   ├── visual.py            # 生成图片卡片（SD + Pillow）
│   ├── report.py            # 组装最终报告（模板渲染 + LLM 包装 + 事实校验）
│   └── community.py         # 社区检测（Louvain 算法）
│
├── services/                # 基础设施层（无业务逻辑，只封装外部系统）
│   ├── reddit_service.py    # Reddit 公开 JSON API（无需密钥）
│   ├── telegram_service.py  # Telegram MTProto（需 API ID/Hash）
│   ├── x_api_service.py     # X (Twitter) API（需 Bearer Token）
│   ├── chroma_service.py    # 向量数据库（帖子 / 声明 / 文章 三个集合）
│   ├── kuzu_service.py      # 知识图谱（账号→帖子→声明→话题→证据）
│   ├── postgres_service.py  # 关系型数据库（结构化存储）
│   ├── embeddings_service.py # OpenAI text-embedding-3-small
│   ├── news_search_service.py    # DuckDuckGo → 权威新闻文章（tier=news）
│   ├── wikipedia_service.py      # Wikipedia REST API（tier=wikipedia）★P0-3
│   ├── claude_vision_service.py  # Claude Vision：图片 OCR + 描述
│   ├── stable_diffusion_service.py # SD 生成图片背景
│   ├── whisper_service.py   # 视频转文字（Whisper）
│   ├── monitor_service.py   # Watch 模式定时监控
│   ├── counter_effect_service.py # 反制效果闭环追踪（SQLite）
│   ├── manifest_service.py  # ★P0-2 run_id 产出 + run_manifest.json
│   └── metrics_service.py   # ★P0-5 量化指标计算 + metrics.json
│
├── models/                  # 数据结构定义（Pydantic）
│   ├── post.py              # Post、ImageAsset
│   ├── claim.py             # Claim、ClaimEvidence（含 source_tier）
│   ├── report.py            # IncidentReport、PropagationSummary、TopicSummary
│   ├── risk_assessment.py   # RiskAssessment、RiskLevel
│   ├── community.py         # CommunityAnalysis、EchoChamberScore
│   ├── persuasion.py        # PersuasionFeatures、CascadePrediction、NamedEntity
│   ├── immunity.py          # ImmunityStrategy、ImmunizationTarget
│   ├── counter_effect.py    # CounterEffectRecord、CounterEffectReport
│   └── manifest.py          # ★P0-2 RunManifest
│
├── api/                     # ★P0-6 FastAPI 只读查询层（禁止 import agents/services）
│   ├── app.py               # FastAPI 入口；CORS 限 127.0.0.1:8501
│   └── routes/
│       ├── runs.py          # GET /runs, GET /runs/{run_id}
│       └── artifacts.py     # /report /raw /metrics /visual/{filename}
│
├── ui/                      # ★P0-6 Streamlit 研究型前端
│   ├── streamlit_app.py     # 入口 + 健康检查
│   ├── api_client.py        # 对 api/ 的 requests 封装
│   ├── pages/
│   │   ├── 1_Run_List.py    # 所有 run 的摘要表
│   │   └── 2_Run_Detail.py  # 单个 run 5 个 tab（Report / Community / Emotion / Counter-visuals / Raw JSON）
│   └── components/
│       ├── metric_cards.py
│       ├── community_graph.py
│       └── emotion_chart.py
│
├── db/
│   ├── schema.sql           # PostgreSQL 建表脚本
│   └── migrate.py           # 执行迁移：python db/migrate.py
│
├── scripts/
│   ├── seed_knowledge.py    # 预热：向 Chroma 批量导入已知文章
│   ├── telegram_login.py    # 首次 Telegram 登录（生成 session 文件）
│   └── telegram_find_channels.py # 搜索 Telegram 频道
│
├── data/                    # 运行时数据（gitignore）
│   ├── runs/                # ★P0-2 每次 run 的 artifact 目录
│   │   └── {run_id}/        # run_id = YYYYMMDD-HHMMSS-{6位hash}
│   │       ├── run_manifest.json
│   │       ├── report.md
│   │       ├── report_raw.json
│   │       ├── metrics.json
│   │       └── counter_visuals/*.png
│   ├── chroma/              # 向量数据库持久化
│   ├── kuzu_graph/          # 知识图谱持久化
│   ├── counter_visuals/     # 老的全局图片目录（向后兼容回落）
│   ├── counter_effects.db   # 反制效果 SQLite 数据库（跨 run 持久化）
│   └── raw_media/           # 下载的媒体文件
│
├── project_report.tex       # 学术项目报告（LaTeX 单文件）
├── PROJECT_ANALYSIS_AND_PLAN.md # P0/P1 修改方案
├── ROADMAP.md               # 路线图
└── .env                     # 密钥配置（不提交 git）
```

---

## 三、数据存储说明

系统使用**四种数据库 + 一类 artifact 目录**，各有分工：

| 存储 | 类型 | 存储内容 | 用途 |
|--------|------|----------|------|
| **Chroma** | 向量数据库（本地文件） | 帖子 / 声明 / 文章的向量嵌入 | 语义相似度搜索、证据召回 |
| **Kuzu** | 图数据库（本地文件） | 账号→帖子→声明→话题→证据 的关系图 | 传播路径、协调行为、社区检测 |
| **PostgreSQL** | 关系型数据库（Docker） | 帖子、账号、声明、报告的结构化数据 | 持久化存储、统计查询 |
| **SQLite** | 轻量文件数据库 | 反制消息部署记录和效果数据 | 跨 run 的闭环效果追踪 |
| **`data/runs/{run_id}/`** | 文件目录 | 每次 run 的 5 件 artifact | 可复现性、UI 只读数据源 |

---

## 四、完整运行流程（24 个 stage）

流水线由 `PlannerAgent.run()` 按顺序编排，每个阶段的结果都写进结构化的 `IncidentReport`，最后由 `ReportAgent` 模板渲染成 Markdown。

```
┌─────────────────────────────────────────────────────────┐
│ 0. ManifestService.new_run()                            │
│    → 创建 data/runs/{run_id}/，记录输入参数/模型/阈值/git │
└────────────────────────┬────────────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────────────┐
│ 1. 意图分类（Claude LLM）                                │
│    → TREND_ANALYSIS / CLAIM_ANALYSIS / COUNTER_MESSAGE  │
└────────────────────────┬────────────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────────────┐
│ 2. 数据采集（IngestionAgent）                            │
│    Reddit(--subreddit) > Telegram > X API > JSONL        │
│    → 帖子存入 Chroma + Kuzu + PostgreSQL                │
│    → posts_snapshot_sha256 计入 manifest                │
└────────────────────────┬────────────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────────────┐
│ 3. 情绪分类（前 50 帖 · fear/anger/hope/disgust/neutral）│
│ 3b. 声明提取 + 两阶段去重（向量 0.92 + LLM 判断）         │
└────────────────────────┬────────────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────────────┐
│ 4. 传播分析（AnalysisAgent）                             │
│    速度、立场分布、异常、协调对、账号角色分类             │
└────────────────────────┬────────────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────────────┐
│ 5. 话题聚类（LLM）+ 话题级指标/情绪分布                  │
└────────────────────────┬────────────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────────────┐
│ 6. 三级证据检索 ★P0-3                                    │
│    6a. News 抓取 → Chroma articles（预热）              │
│    6b. Chroma 向量匹配 → tier=internal_chroma            │
│    6c. 若零证据：Wikipedia → tier=wikipedia              │
│    6d. 若零证据且有 NEWSAPI_KEY：NewsAPI → tier=news    │
│    ClaimEvidence.source_tier 标注来源                   │
└────────────────────────┬────────────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────────────┐
│ 7. 风险评估 + 风险门控（RiskAgent）                      │
│    → misinfo_score + RiskLevel                          │
│    → INSUFFICIENT_EVIDENCE 路由人工审核                 │
└────────────────────────┬────────────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────────────┐
│ 8. 反制消息生成 ★P0-4 改进：actionable 门控             │
│    触发条件：primary_claim.has_actionable_counter_       │
│    evidence() 即 ≥1 contradicting_evidence              │
│    否则 → skip；counter_message_skip_reason 记录原因：   │
│      no_primary_claim / no_risk_assessment /           │
│      insufficient_evidence / no_actionable_counter_     │
│      evidence / risk_gate_not_triggered / unknown       │
│    生成后 CriticAgent 审核，最多重试 2 次                │
└────────────────────────┬────────────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────────────┐
│ 9. 并行阶段：                                            │
│    9a. Visual Card（SD 背景 + Pillow 文字）              │
│    9b. Topic Cards（每个热门话题一张）                   │
│    9c. 社区检测（Louvain + modularity Q）               │
│    9d. 级联预测（24h 传播量）                            │
│    9e. 说服手法识别                                      │
│    9f. 实体提取 + 共现                                   │
│    9g. 反制定向计划                                      │
│    9h. 免疫策略推荐                                      │
│    9i. ★P0-4 反制效果闭环：                             │
│        · 先扫描 get_pending_followups()                 │
│        · 对匹配当前 topic/claim 的 PENDING 调用          │
│          record_followup() 生成 effect_score → 转       │
│          EFFECTIVE / NEUTRAL / BACKFIRED                │
│        · 同时 record_deployment() 新 baseline           │
└────────────────────────┬────────────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────────────┐
│ 10. 报告生成 ★P0-1 模板化                               │
│     _render_body(report_obj) → 100% 代码渲染所有         │
│     结构性段落（Claim / Evidence / Propagation /        │
│     Emotion / Risk / Cascade / Persuasion / Community / │
│     Counter-Messaging / Immunity / Counter-Effect /     │
│     Run Metrics）                                       │
│     _llm_wrap() 仅写 Executive Summary + Flags and      │
│     Next Steps 两节（temp=0.3, max_tokens=512）          │
│     _verify_report_facts() 正则扫 LLM 段与结构化         │
│     对象对比；不一致 → 回落到纯模板渲染                  │
│     → 写 run_dir/report.md + report_raw.json            │
└────────────────────────┬────────────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────────────┐
│ 11. MetricsService.compute() + write() ★P0-5           │
│     evidence_coverage / evidence_tier_distribution /    │
│     community_modularity_q / account_role_counts /      │
│     counter_effect_closed_loop_rate /                   │
│     actionable_counter_evidence_rate / risk_level /     │
│     post_count / topic_count / counter_message_         │
│     deployed / counter_message_skip_reason              │
│     → 写 run_dir/metrics.json                           │
└────────────────────────┬────────────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────────────┐
│ 12. ManifestService.finalize()                          │
│     → posts_snapshot_sha256 + finished_at + report_id   │
│     → 写 run_dir/run_manifest.json                      │
└─────────────────────────────────────────────────────────┘
```

---

## 五、CLI 使用方式

```bash
cd society-analysis-project-update

# 分析 Reddit 某 subreddit（最常用）
python main.py \
  --subreddit "worldnews" \
  --query "Analyze trending misinformation on worldnews"

# 运行后将在 data/runs/{run_id}/ 下产出 5 件 artifact
# 无需传 --output-json：默认写到 run_dir/report_raw.json

# 若仍想输出到指定路径（向后兼容）
python main.py \
  --subreddit "worldnews" \
  --output-json ./output.json \
  --output-md ./report.md
# 此时同时写 run_dir 和指定路径

# 多 subreddit
python main.py --subreddit "worldnews,politics,conspiracy"

# Telegram
python main.py --channel "RealHealthRanger"

# 离线 JSONL
python main.py --query "vaccine misinformation" --jsonl data/sample_posts.jsonl

# Watch 模式
python main.py --subreddit "worldnews" --watch --interval 300
```

**退出码：** `0` 正常；`1` 需要人工审核（INSUFFICIENT_EVIDENCE 或高风险）

---

## 六、研究型 UI（P0-6）

两个进程，只读 `data/runs/`：

```bash
# 终端 1：启动 FastAPI
uvicorn api.app:app --port 8000

# 终端 2：启动 Streamlit
streamlit run ui/streamlit_app.py
# 浏览器打开 http://localhost:8501
```

**页面：**
- **Run List** — 所有 run 的摘要表（run_id / started_at / query / posts / metrics）
- **Run Detail** — 选中某 run 后展开 5 个 tab：
  - Report：`report.md` 原文
  - Community：社区列表、size、dominant_emotion、echo_chamber、accounts
  - Emotion：各话题的情绪分布堆叠柱状图（Plotly）
  - Counter-visuals：本次生成的 PNG
  - Raw JSON：完整 `report_raw.json`
- 顶部指标卡：evidence_coverage / community_Q / closed_loop_rate / post_count

**架构约束：** `api/` 与 `ui/` 禁止 `import agents/*` 或 `services/*` —— UI 与业务逻辑完全解耦，只能通过文件系统读 artifact。

**API 端点：**
- `GET /runs` — 所有 run 的摘要
- `GET /runs/{run_id}` — manifest + metrics + artifact 存在标志
- `GET /runs/{run_id}/report` — report.md 原文
- `GET /runs/{run_id}/raw` — report_raw.json
- `GET /runs/{run_id}/metrics` — metrics.json
- `GET /runs/{run_id}/visual/{filename}` — PNG（含路径穿越防护）

---

## 七、配置（.env 文件）

```env
# 必填
ANTHROPIC_API_KEY=sk-ant-...

# PostgreSQL（Docker 容器）
POSTGRES_DSN=postgresql://society:society_pass@localhost:5432/society_db

# 可选：Reddit 代理（如果被封锁）
REDDIT_PROXY=http://127.0.0.1:7890

# 可选：Telegram
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=abcdef...

# 可选：X (Twitter) API
X_BEARER_TOKEN=...

# 可选：OpenAI 嵌入（不填则降级到 sentence-transformers）
OPENAI_API_KEY=sk-...

# 可选：NewsAPI（启用 tier=news 兜底检索）
NEWSAPI_KEY=...

# 可选：UI 指向自定义 runs 目录
RUNS_DIR=D:\path\to\custom\runs
# API 供 UI 调用的基地址（ui/api_client.py 读取）
RESEARCH_API_BASE=http://127.0.0.1:8000
```

---

## 八、数据源优先级

```
--subreddit 参数 → reddit_service.py（公开 API，无需密钥）
--channel 参数   → telegram_service.py（需配置凭据）
--query 参数     → Telegram(若配置) → X API(若配置) → 空列表
--jsonl 参数     → x_api_service.load_from_jsonl()（离线）
```

---

## 九、输出文件说明

**每次 run 的 artifact 目录（推荐以此为准）：**

| 文件 | 内容 |
|------|------|
| `data/runs/{run_id}/run_manifest.json` | 输入参数、模型、阈值、git sha、posts 快照哈希 |
| `data/runs/{run_id}/report.md` | 模板渲染的 Markdown 报告 |
| `data/runs/{run_id}/report_raw.json` | 完整 IncidentReport（Pydantic model_dump） |
| `data/runs/{run_id}/metrics.json` | 量化指标 |
| `data/runs/{run_id}/counter_visuals/*.png` | 本次生成的图片卡片 |

**跨 run 持久化：**

| 文件 | 内容 |
|------|------|
| `data/chroma/` | 向量数据库（帖子 / 声明 / 文章） |
| `data/kuzu_graph/` | 知识图谱（账号/帖子/声明/话题/证据） |
| `data/counter_effects.db` | 反制效果 SQLite 数据库（闭环追踪依赖） |

**向后兼容的全局路径（若用户显式指定）：**
`--output-json` / `--output-md` 指向任意路径；`data/counter_visuals/` 作为回落目录（run_dir 为 None 时使用）。

---

## 十、metrics.json 字段说明

| 字段 | 含义 |
|------|------|
| `evidence_coverage` | 有至少 1 条证据的 claim 占比 ∈ [0,1] |
| `evidence_with_any` / `evidence_total_claims` | 分子/分母 |
| `evidence_tier_distribution` | `{internal_chroma, wikipedia, news}` 三级来源计数 |
| `community_modularity_q` | Louvain Q 值；社区检测未启用时为 null |
| `account_role_counts` | `{ORIGINATOR, AMPLIFIER, BRIDGE, PASSIVE, ...}` |
| `counter_effect_closed_loop_rate` | 跨 run 闭环率 `(total - pending) / total` |
| `counter_effect_summary` | total_tracked / effective / neutral / backfired / 均分 |
| `counter_message_deployed` | 本次是否部署了反制消息（bool） |
| `counter_message_skip_reason` | 若未部署，原因枚举 |
| `actionable_counter_evidence_rate` | 有 ≥1 contradicting 的 claim 占比 — 诊断立场分类器是否过度保守 |
| `risk_level` / `post_count` / `topic_count` | 基础统计 |

---

## 十一、各阶段状态

| status | 含义 |
|--------|------|
| `ok` | 正常完成 |
| `degraded` | 完成但结果不完整 |
| `error` | 异常，但流程继续（非致命） |
| `blocked` | 门控拦截，流程提前退出（如 counter_message 被 actionable 门控） |

---

## 十二、快速启动（首次部署）

```bash
# 1. 启动 PostgreSQL
docker run -d --name society-pg --restart unless-stopped \
  -e POSTGRES_USER=society -e POSTGRES_PASSWORD=society_pass \
  -e POSTGRES_DB=society_db -p 5432:5432 postgres:16

# 2. 数据库迁移
python db/migrate.py

# 3. 配置 .env（至少 ANTHROPIC_API_KEY）

# 4. 安装依赖
pip install -e .

# 5. 第一次 run
python main.py --subreddit "worldnews" \
  --query "Analyze trending misinformation on worldnews"

# 6. （可选）启动研究 UI
uvicorn api.app:app --port 8000 &
streamlit run ui/streamlit_app.py
```

---

## 十三、Phase 功能对照

| Phase | 功能 | 状态 |
|-------|------|-----|
| **Phase 0** | 情绪分类、账号角色分类 | ✅ 完成 |
| **Phase 1** | 社区检测（Louvain）、回音室、影响力 | ✅ 完成 |
| **Phase 2** | 级联预测、说服手法、实体提取、反制定向 | ✅ 完成 |
| **Phase 3** | 免疫策略、反制效果闭环追踪 | ✅ 完成 |
| **P0-1** | report 模板化 + LLM 仅包装摘要 + 事实校验 | ✅ 完成 |
| **P0-2** | run_manifest + `data/runs/{run_id}/` 目录 | ✅ 完成 |
| **P0-3** | Wikipedia 三级证据召回 + source_tier | ✅ 完成 |
| **P0-4** | counter_effect 跨 run 闭环 | ✅ 完成 |
| **P0-5** | metrics.json（11 项指标） | ✅ 完成 |
| **P0-6** | 研究型 UI（FastAPI + Streamlit） | ✅ 完成 |
| **P0 额外** | actionable counter-evidence 门控 + skip_reason | ✅ 完成 |
| **P1-1** | Trust score（`agents/trust.py`） | ⏳ 下一轮 |
| **P1-2** | persuasion n-gram 引用 | ⏳ 下一轮 |
| **P1-3** | `--dry-run` / `--skip-stage` CLI 开关 | ⏳ 下一轮 |
| **P1-4** | Claim Inspector + 人工审阅 UI + `claim_reviews` 表 | ⏳ 下一轮 |

---

## 十四、信任层设计要点

P0-1 的核心理念是**消除 LLM 对结构性结论的污染**，具体通过三道防线：

1. **模板渲染** — `_render_body()` 把所有带数字/枚举的段落（claim 列表、证据 tier 分布、社区数/Q 值、速度、outcome 枚举等）100% 从结构化 `IncidentReport` 对象生成，LLM 接触不到这些字段。
2. **LLM 受限** — 只让 LLM 写 Executive Summary 和 Flags and Next Steps 两节，`temperature=0.3`、`max_tokens=512`，system prompt 明确禁止修改其他节。
3. **事实校验** — `_verify_report_facts()` 用正则扫 LLM 输出中的关键字段（community_count / post_count / velocity / effect_score / outcome 枚举），与结构化对象对比；差异 > 0 时 `log.error("report.fact_drift", ...)` 并回落到纯模板渲染。

配合 `metrics.json` 里的 `actionable_counter_evidence_rate` 诊断指标，可以独立观察到**立场分类器是否把所有证据都路由到 "uncertain"**（高 evidence_coverage + 低 actionable_rate 即是信号），而不是等出现反制消息空洞化才发现。

# 项目工作流程与架构

> **状态**：v2 主干 + Phase 6 上下文 + KG 改造（Phase A→D）+ 8 天生产化交付
> **日期**：2026-05-03
> **测试**：142 passed / 1 skipped
> **依据**：当前代码 + 三库（Postgres / Kuzu / Chroma×3）实际接线

本文档面向开发者和测试者，描述当前项目"怎么跑、谁调谁、数据流向哪里"。
设计文档见 `PROJECT_REDESIGN_V2.md`，分阶段交付总结见 `docs/phase{1..5}_done.md`。

---

## 0. 一张图看全部

```
                       ┌───────────────────────────────────────┐
                       │       Postgres (society_db)           │
                       │  posts_v2 / topics_v2 / entities_v2 / │
                       │  post_entities_v2 / schema_meta /     │
                       │  reflection_log                       │
                       └───────────────────────────────────────┘
                                       ▲
              ┌────────────────────────┼────────────────────────┐
              │                        │                        │
   ┌──────────┴──────────┐  ┌──────────┴──────────┐  ┌──────────┴──────────┐
   │   Kuzu graph        │  │   Chroma 1          │  │   Chroma 2 / 3      │
   │ User-Post-Topic-    │  │   chroma_official   │  │ chroma_nl2sql:      │
   │ Entity / Posted /   │  │ (BBC NYT Reuters    │  │   schema docs +     │
   │ Replied / Liked /   │  │  AP Xinhua chunks)  │  │   NL→SQL exemplars  │
   │ HasEntity ...       │  │                     │  │ chroma_planner:     │
   │                     │  │                     │  │   ModuleCards +     │
   │                     │  │                     │  │   workflow examples │
   └──────────▲──────────┘  └──────────▲──────────┘  └──────────▲──────────┘
              │                        │                        │
   ───────────┴────────────────────────┴────────────────────────┴───────────
                                       ▲
                       ┌───────────────┴───────────────┐
                       │   后台 Pipeline (offline)     │
                       │   PrecomputePipelineV2 +      │
                       │   OfficialIngestionPipeline   │
                       └───────────────┬───────────────┘
                                       │
                                       │
                       ┌───────────────┴───────────────┐
                       │   前台 Pipeline (chat)         │
                       │   ChatOrchestrator (v2)       │
                       │   Rewriter → Planner → ...    │
                       └───────────────────────────────┘
```

---

## 1. 后台 Pipeline（数据沉淀）

### 1.1 社区数据臂 — `python main.py --jsonl ... | --subreddit ...`

`agents/precompute_pipeline_v2.py::PrecomputePipelineV2` 串 7 个 stage：

| # | Stage              | 实现                             | 产物 |
|---|--------------------|----------------------------------|------|
| 1 | fetch_posts        | `agents/ingestion.py`            | 原始 Post |
| 2 | ingest             | multimodal + entity_extract + simhash + dedup | Post.entities, Post.simhash |
| 3 | normalize          | `_normalize` 字段兜底             | Post.id/account_id 非空 |
| 4 | emotion_baseline   | `agents/knowledge.py::KnowledgeAgent.classify_post_emotions` | Post.emotion / emotion_score |
| 5 | topic_cluster      | `agents/topic_clusterer.py` (KMeans + LLM 标签) | Post.topic_id, TopicCluster.label |
| 6 | schema_propose     | `agents/schema_agent.py` + `services/schema_sync.py` | schema_meta + Chroma 2 (kind=schema) |
| 7 | persist_v2         | 写 PG `posts_v2 / topics_v2 / entities_v2 / post_entities_v2` + Kuzu 节点边 | 三库一致 |

**多模态采样规则**（PROJECT_REDESIGN_V2.md Q6=B）：
- 只对 `like_count >= MULTIMODAL_MIN_LIKES (50)` 或 `reply_count >= MULTIMODAL_MIN_REPLIES (20)` 的帖子做图像理解
- `MULTIMODAL_DAILY_BUDGET_USD=5.0` 配额超过自动跳过

**Schema 双写契约**（PROJECT_REDESIGN_V2.md Phase 2 5b）：
- SchemaProposal 包含 4 张 v2 表的所有 core 列
- `SchemaSync.apply_proposal` 在一次循环里：
  1. 逐列 upsert PG `schema_meta`（包含 sha256 fingerprint）
  2. 逐列 upsert Chroma 2 `kind=schema`（id 形如 `schema::posts_v2::topic_id`）
  3. 删 Chroma 2 中本次不出现的孤儿 schema doc
- 不一致时 `services/schema_sync.SchemaSync.verify()` 会报告 missing/orphan/fingerprint_drift

**Post 去重**：simhash 64-bit + Hamming ≤ 3；> 500 token 长文 pg_trgm 兜底（≥ 0.85）

### 1.2 官方源臂 — `python -m agents.official_ingestion_pipeline --once`

`agents/official_ingestion_pipeline.py::OfficialIngestionPipeline` 为 5 个白名单源拉 RSS：

| 源 | 接入方式 | 当前真实 chunk 数 |
|---|---|---|
| BBC | RSS 直连 | 61 |
| NYT | RSS 直连 | 40 |
| Reuters | Google News RSS 代理 (`site:reuters.com`) | 20 |
| AP | Google News RSS 代理 (`site:apnews.com`) | 20 |
| Xinhua | RSS 直连 | 28 |

流程：RSS → 清洗 → 800/200 token 切 chunk → metadata → 写入 `data/official_chunks/{date}/{source}.jsonl` 同时 embedding 后 upsert 到 Chroma 1 (`chroma_official`)。

辅助命令：
- `--replay [--date 2026-05-02]` 把已有 jsonl 回放到 Chroma 1（断网或回填用）
- `--no-chroma` 只写 jsonl
- `--source bbc` 只跑单源
- `--list` 看启用的源

### 1.3 Chroma 3 冷启动 — `python -m scripts.seed_planner_memory`

把 3 张 ModuleCard（evidence / nl2sql / kg 各一）+ 8 条 WorkflowExemplar 写到 Chroma 3 (`chroma_planner`)。
LLM Planner 启动时按 Q9 置信度规则按需召回。

### 1.4 衰减扫描 — `python -m scripts.decay_chroma_experience` （建议 cron 每天）

扫 Chroma 2 / Chroma 3，删除：
- `last_used_at` > 30 天前的 `success / error / workflow_*` 文档
- `confidence < 0.2` 的文档

`kind=schema` / `kind=module_card` 是 anchor 文档，永不删。

---

## 2. 前台 Pipeline（实时问答）

入口：`POST /chat/query`（FastAPI）→ `agents/chat_orchestrator.py::ChatOrchestrator.handle()`

```
load session JSON
  → QueryRewriter.rewrite(message, session)             [LLM #1]
      → 输出 RewrittenQuery（1-3 个 Subtask + 继承的 current_run/topic/claim_id）
  → BoundedPlannerV2.plan_and_execute(rq)
      → 对每个 Subtask:
          1. TopicResolver.resolve(text)               [embedding 相似度]
              -> 返回 top-K 语义匹配的 topic_id
          2. _BranchRouter.route(subtask)
              -> 根据 intent + suggested_branches 决定调哪些分支
          3. 并行执行（max 3 分支）:
              ├─ tools/hybrid_retrieval.py             [Chroma 1]
              │     Metadata + Dense + BM25 + RRF + Rerank
              ├─ tools/nl2sql_tools.py                 [Chroma 2 + Postgres]
              │     Recall schema/exemplars/errors → LLM SQL → 安全沙箱执行
              │     失败时 repair loop 最多 3 轮
              └─ tools/kg_query_tools.py               [Kuzu]
                    propagation_path / key_nodes /
                    community_relations / topic_correlation
      → PlanExecutionV2（含每分支 success/error/output）
  → ReportWriter.write(rq, execution)                   [LLM #2]
      → 综合三源 → markdown_body + ReportNumber + Citation
      → 后处理：把 raw topic_id/entity_id 替换为 label/name
  → QualityCritic.review(report, execution)             [LLM #3, 可跳过]
      → 4 轴校验:
          ① citation 完整性  (程序化)
          ② 数字一致性      (程序化, 对比 SQL/KG 实际值)
          ③ on_topic       (LLM)
          ④ hallucination  (LLM)
      → 失败 retry 1 次
      → 二次失败 → report.needs_human_review=True
  → ReflectionStore.record(verdict, ...)
      → AblationRunner（per-turn）替换为真实因果验证器
      → 错误归因路由:
          sql_empty_result    → Chroma 2 `kind=error`
          missing_branch      → Chroma 3 `kind=workflow_error`
          wrong_branch_combo  → Chroma 3 `kind=workflow_error`
          citation_missing    → Chroma 3 `kind=composition_error`
          numeric_mismatch    → Chroma 3 `kind=composition_error`
      → 同时写 PG `reflection_log` 审计表
  → save session + return ChatResponse
      ⤷ session_store.save() 内部：
          conversation_compactor.maybe_compact(state)
          - 若 len(conversation) > SESSION_MAX_TURNS (默认 40)
            把最老的 ≥ MIN_TURNS_TO_COMPACT (默认 10) 轮喂给 LLM 压缩
          - state.summary 滚动更新（≤ 1200 字符）
          - state.archived_count / summary_until_turn 计数
          - LLM 失败 → 仍截断（path A），不阻塞写盘
```

LLM 调用预算：
- 每次 chat 主流程最多 3 次（rewriter + writer + critic）
- 摊销开销：每 10 轮 1 次 conversation_compactor 调用（≈ $0.0001/turn）
- 期望延迟 5-8s（compactor 不在主路径，是 save 阶段的旁路）

### 2.1 多分支默认路由

`agents/planner_v2.py::_BranchRouter.intent_branch_map` —— 默认就 fan-out：

| Subtask intent       | 默认分支组合 |
|----------------------|--------------|
| `community_count`    | `[nl2sql]`（纯聚合，单分支足够）|
| `community_listing`  | `[nl2sql, kg]` |
| `trend`              | `[nl2sql, kg]` |
| `propagation`        | `[kg, nl2sql]` |
| `fact_check`         | `[evidence, nl2sql]` |
| `official_recap`     | `[evidence, nl2sql]` |
| `comparison`         | `[evidence, nl2sql, kg]`（3 路全开）|
| `explain_decision`   | `[nl2sql, kg]` |
| `freeform`           | `[evidence, nl2sql]` |

Rewriter 出的 `suggested_branches` 优先级最高。

### 2.2 语义话题解析（关键改进）

用户问"vaccine debate"或"global news"时，PG 里的 topic label 可能是
"Media Trust and Vaccine Information"或"Mixed Economic and Climate News"，字面匹配会失败。

`tools/topic_resolver.py::TopicResolver`：
1. 拉所有 topic 的 `label || centroid_text`
2. embed → 与用户查询的 embedding 算 cosine
3. 三段式过滤：
   - 绝对底线 `min_similarity=0.22`
   - 相对 gap：`top_sim / 1.5` 以下丢掉
   - top-1 兜底
4. 返回 top-3 匹配 topic_id

Planner 把结果以 `topic_id_hints` 注入 NL2SQL，prompt 显式要求
`WHERE posts_v2.topic_id IN ('topic_xxx', 'topic_yyy')`，绕开 LIKE 匹配。

### 2.3 NL2SQL 安全约束

`tools/nl2sql_tools.py::_sanitise_sql`：
- 仅允许 `WITH` / `SELECT` 起手
- 禁止多语句（任何 `;` 拒绝）
- 禁止 `INSERT/UPDATE/DELETE/DROP/ALTER/...`
- 强制 `LIMIT NL2SQL_RESULT_ROW_LIMIT (1000)`
- `SET LOCAL statement_timeout = NL2SQL_STATEMENT_TIMEOUT_MS (5000)`
- `SET TRANSACTION READ ONLY`
- 推荐配置 `POSTGRES_READONLY_DSN` 用专用只读账号

### 2.4 上下文窗口管理（Phase 6, A + B）

`SessionState.conversation` 不再无限增长：

```
conversation 长度 < SESSION_MAX_TURNS (40)         → 不动
conversation 长度 ≥ SESSION_MAX_TURNS              → 触发压缩
   - 取最老的 max(excess, MIN_TURNS_TO_COMPACT=10) 轮
   - LLM 调用 (1 次, ~400 token output) 把它们 + 已有 state.summary 合并
   - 写回 state.summary (≤ SESSION_SUMMARY_MAX_CHARS=1200)
   - 从 conversation 里 pop 这些轮
   - state.archived_count / summary_until_turn 计数推进
```

**Rewriter 怎么看到老内容**：
- 窗口内的 3 条最近 assistant 回复仍由 `recent_assistants` 提供
- 窗口外的内容由 `older_context_summary`（来自 `state.summary`）提供
- 第 41 轮第一次触发，第 51 / 61 / ... 轮后续滚动 merge

**失败模式**：LLM 不可达时退化为纯 trim（path A）—— summary 不更新但 session 文件仍封顶大小，不影响主对话流。

### 2.5 错误归因层级

| 层 | 触发位置 | 处理 | 是否进 Reflection |
|---|---|---|---|
| L1 | NL2SQL 内部 (sql_syntax / unknown_column / timeout / ...) | repair loop 自纠 max 3 轮；3 轮失败写 Chroma 2 kind=error | ❌ |
| L2 | Critic (sql_empty_result / missing_branch / citation_missing / numeric_mismatch / off_topic) | ReflectionStore 路由到 Chroma 2 或 Chroma 3 | ✅ |

### 2.6 KG 分支（Phase A→D）

KG 分支不再是"等价 SQL 视图"——它独占 5 类查询：

| Query kind | 实现 | 算法/Cypher |
|---|---|---|
| `propagation_path` | `tools/kg_query_tools.py` | Post-Post Reply 双向 k-hop |
| `cascade_tree` | `tools/kg_query_tools.py` | 递归 reply 树 |
| `viral_cascade` | `tools/kg_query_tools.py` | Cypher + ranking |
| `influencer_rank` | `agents/kg_analytics.py` | NetworkX PageRank |
| `bridge_accounts` | `agents/kg_analytics.py` | NetworkX betweenness |
| `coordinated_groups` | `agents/kg_analytics.py` | NetworkX Louvain |
| `echo_chamber` | `agents/kg_analytics.py` | Modularity 阈值 |

5 个新 SubtaskIntent (`propagation_trace / influencer_query / coordination_check / community_structure / cascade_query`) 自动路由到对应 KG 算法。

子图缓存 `services/kg_cache.py` LRU(8) — pipeline 写完后 `bump_write_seq()` 自动失效。

### 2.7 上下文管理（生产硬约束）

- **Session 滚动窗口**（Phase 6）：`SESSION_MAX_TURNS=40`，超出走 LLM compactor 滚动 summary
- **BM25 缓存**（Day 1）：`services/bm25_cache.py` LRU(4)，OfficialIngestionPipeline 写入后 `bump_corpus_version()`
- **KG 子图缓存**（Phase B.4）：`services/kg_cache.py` LRU(8)，pipeline persist 完毕调 `bump_write_seq()`
- **NL2SQL freshness**（Day 7）：trending/propagation 默认 `WHERE posted_at >= NOW() - INTERVAL '30 days'`，fact_check 不限
- **KG 子图按时间窗切片**（Day 7）：`influencer_rank / coordinated_groups` 默认 `since_days=30`，先用 PG 过滤 post_id 再传给 Cypher

### 2.8 Run lineage + 事务回滚（Day 4）

- 每行 PG 数据携带 `first_seen_in_run / last_updated_in_run`
- `RunManifest.commit_state` ∈ `{pending, committed, failed}`
- main.py 启动时扫 `data/runs/` 把任何 `pending/failed` run 通过 `data_admin._rollback_one` 删除
- `topic_id` 用内容指纹 `sha256(sorted member post_ids)[:12]` —— 跨 run 稳定
- Chroma 1 chunk metadata 加 `source_run_id`，`data_admin rollback` 用 `where={"source_run_id": run_id}` 反删

### 2.9 Kuzu 单 writer（Day 5）

- `KuzuService(read_only=True)` 是默认值；reader（API / UI / health endpoint）走只读
- 显式 writer：`main.py::_build_ingestion` 和 `scripts/scheduler.py` 的 pipeline 任务
- 解锁多 worker chat 部署，pipeline 跑时 chat 仍可读

### 2.10 KG 批量写入（Day 6）

- `KuzuService.bulk_upsert_accounts / bulk_upsert_posts / bulk_add_posted / bulk_add_replied / bulk_add_belongs_to_topic`
- `precompute_pipeline_v2._persist_v2` 重写为"先聚集 rows 列表，再分表批量"
- 1000 帖批次：cypher 调用从 4000+ 降到 5（每 bulk 入口一次集中循环）

### 2.11 Metrics（Day 8）

- `services/metrics.py` 自建 Counter / Histogram，无 prometheus 依赖
- 关键打点：`chat.calls / rewriter.latency_ms / rewriter.subtasks / planner.latency_ms / planner.branches / nl2sql.calls / nl2sql.repair_rounds / nl2sql.latency_ms / critic.verdict / critic.error_kind`
- `/health/metrics` JSON 快照
- Reflection 看板第 5 tab "Performance" 显示 counters + p50/p90/p99

---

## 3. 数据存储一览

### 3.1 Postgres `society_db` (DSN in `config.POSTGRES_DSN`)

| Table              | 用途 |
|--------------------|------|
| `posts_v2`         | 帖子主表，15 个 core 列 + JSONB extra + simhash + tsvector |
| `topics_v2`        | 话题（cluster + label + centroid + dominant_emotion） |
| `entities_v2`      | 实体（PERSON / ORG / LOC / EVENT / OTHER） |
| `post_entities_v2` | post ↔ entity 关联（含 char_start/end + confidence） |
| `schema_meta`      | per-column 描述 + sha256 fingerprint（双写到 Chroma 2） |
| `reflection_log`   | Critic 拒绝事件审计 |

应用 schema：`python -c "import psycopg2,config; psycopg2.connect(config.POSTGRES_DSN).cursor().execute(open('db/schema_v2.sql').read())"`

### 3.2 Kuzu graph (in `data/kuzu_graph`)

节点：`Account / Post / Topic / Entity`（v1 也定义了 `Claim / Article / FactCheck / Community / ImageAsset`，v2 不再写入）
v2 关系：`Posted / Replied / Liked / BelongsToTopic / HasEntity`
入口：`services/kuzu_service.py`

### 3.3 Chroma 三库 (in `data/chroma`)

| Collection         | 内容 | 写入者 | 读取者 |
|--------------------|------|--------|--------|
| `chroma_official`  | 官方源 chunk + metadata | `OfficialIngestionPipeline` | Hybrid Retrieval (Branch A) |
| `chroma_nl2sql`    | `kind=schema` (Schema Agent) / `kind=success` (Reflection 收成功) / `kind=error` (NL2SQL repair 失败) | SchemaSync + NL2SQLMemory | NL2SQLTool 召回 |
| `chroma_planner`   | `kind=module_card` (3 张) / `kind=workflow_success` (8 条 seed + Reflection 收) / `kind=workflow_error` / `kind=composition_error` | seed 脚本 + ReflectionStore | TopicResolver + Planner few-shot |

冲突替换 3 段式（PROJECT_REDESIGN_V2.md Q11=B）：
- sim < 0.92 → 追加
- 0.92 ≤ sim < 0.95 → 直接覆盖
- sim ≥ 0.95 → LLM 仲裁

### 3.4 文件系统

- `data/runs/{run_id}/run_manifest_v2.json` —— 每次 PrecomputePipelineV2 跑完写一份
- `data/official_chunks/{date}/{source}.jsonl` —— 官方源原始 chunk 备份
- `data/sessions/{session_id}.json` —— Chat 会话状态（current_run_id / current_topic_id / conversation）

---

## 4. 关键模块清单（按层）

### 4.1 Agents

| 文件 | 角色 |
|---|---|
| `agents/ingestion.py::IngestionAgent` | 抓帖子 + 镜像到 Kuzu |
| `agents/multimodal_agent.py::MultimodalAgent` | 图片 → 文字描述（Claude Vision） |
| `agents/entity_extractor.py::EntityExtractor` | 抽 PERSON / ORG / LOC / EVENT |
| `agents/post_dedup.py::PostDeduper` | simhash 64-bit + pg_trgm 兜底 |
| `agents/knowledge.py::KnowledgeAgent` | 仅情绪分类（v2 slim 版） |
| `agents/topic_clusterer.py::TopicClusterer` | KMeans + LLM 归纳 label |
| `agents/schema_agent.py::SchemaAgent` | 提议 SchemaProposal |
| `agents/precompute_pipeline_v2.py::PrecomputePipelineV2` | 7 stage 后台主干 |
| `agents/official_ingestion_pipeline.py::OfficialIngestionPipeline` | 官方源臂 |
| `agents/query_rewriter.py::QueryRewriter` | 子任务拆分 + 上下文继承 |
| `agents/planner_v2.py::BoundedPlannerV2` | bounded DAG 编排 + 并行执行 |
| `agents/report_writer.py::ReportWriter` | 三源综合 → markdown + citation |
| `agents/quality_critic.py::QualityCritic` | 4 轴校验 |
| `agents/ablation_runner.py::AblationRunner` | per-turn 因果验证 |
| `agents/conversation_compactor.py::maybe_compact` | 滚动窗口 + LLM 压缩老对话（Phase 6） |
| `agents/kg_analytics.py::KGAnalytics` | NetworkX PageRank / Louvain / betweenness / modularity（Phase B.3） |
| `agents/chat_orchestrator.py::ChatOrchestrator` | v2 主入口 |

### 4.2 Tools（atomic 操作）

| 文件 | 角色 |
|---|---|
| `tools/hybrid_retrieval.py::HybridRetriever` | Metadata + Dense + BM25 + RRF + Rerank |
| `tools/nl2sql_tools.py::NL2SQLTool` | 生成 + 校验 + repair |
| `tools/kg_query_tools.py::KGQueryTool` | 4 类 Cypher 查询 |
| `tools/topic_resolver.py::TopicResolver` | 用户话题短语 → topic_id（embedding 相似） |
| `tools/kg_query_tools.py::KGQueryTool` | 5 类 Cypher 查询（含 cascade_tree / viral_cascade）；Post + Account 节点信息 hydrate |

### 4.3 Services（存储 / 第三方封装）

| 文件 | 角色 |
|---|---|
| `services/postgres_service.py::PostgresService` | 全部 PG 操作 |
| `services/kuzu_service.py::KuzuService` | 全部 Kuzu 操作 |
| `services/chroma_collections.py::ChromaCollections` | 三 Chroma 统一封装 |
| `services/embeddings_service.py::EmbeddingsService` | OpenAI text-embedding-3-small |
| `services/nl2sql_memory.py::NL2SQLMemory` | Chroma 2 读写 + 冲突替换 |
| `services/planner_memory.py::PlannerMemory` | Chroma 3 读写 |
| `services/schema_sync.py::SchemaSync` | PG ↔ Chroma 2 一致性 |
| `services/reflection_store.py::ReflectionStore` | 错误归因路由 + ablation 调度 |
| `services/bm25_cache.py` | BM25 索引 LRU(4)（Day 1） |
| `services/kg_cache.py::SUBGRAPH_CACHE` | KG 子图 LRU(8)（Phase B.4） |
| `services/metrics.py::metrics` | Counter / Histogram，自建 metrics（Day 8） |
| `services/session_store.py` | JSON 文件会话存储 |
| `services/manifest_service.py::ManifestService` | run 目录 + manifest |
| `services/claude_vision_service.py::ClaudeVisionService` | 多模态 |
| `services/news_search_service.py::NewsSearchService` | 官方源全文抓取（trusted-domain 过滤） |
| `services/wikipedia_service.py::WikipediaService` | 备用知识源（v2 暂未直接使用） |
| `services/reddit_service.py` | Reddit 社区数据源 |

### 4.4 API（FastAPI）

| 路由 | 用途 |
|---|---|
| `POST /chat/query` | 主对话入口（Orchestrator） |
| `GET /chat/session/{id}` | 读会话状态 |
| `POST /retrieve/evidence` | 直调 Branch A |
| `POST /retrieve/nl2sql` | 直调 Branch B |
| `POST /retrieve/kg` | 直调 Branch C |
| `GET /reflection/{chroma2,chroma3,log}` | 看 Reflection 数据 |
| `DELETE /reflection/{chroma2,chroma3}/{id}` | 手动清理经验 |
| `GET /runs[, /runs/{id}/report, /runs/{id}/metrics]` | run artefact |
| `GET /artifacts/{run_id}/{path}` | 静态文件透传 |
| `GET /health` | 健康检查 |
| `GET /health/kg` | KG 节点 / 边计数 + 缓存命中率 + warnings（Phase D.2） |
| `GET /health/metrics` | Counters + 直方图 quantile JSON 快照（Day 8） |
| `POST /admin/import/reddit` | 从 UI/API 手动触发 Reddit v2 pipeline 后台导入 |
| `POST /admin/import/official` | 从 UI/API 手动触发官方/evidence RSS 导入 |
| `GET /admin/import/jobs/{job_id}` | 查询后台导入任务状态、结果和 warning |

启动：`uvicorn api.app:app --port 8000`

### 4.5 UI（Streamlit）

当前 UI 已收敛为单页 Chat：

| 文件 | 用途 |
|---|---|
| `ui/streamlit_app.py` | 单页 Streamlit 入口：Chat、数据源筛选、导入按钮、建议问题模板 |
| `ui/components/chat_response.py` | 回答下方的 source citation、RAG/KG/NL2SQL 路由模块卡片、KG 图谱可视化、debug JSON |
| `ui/api_client.py` | 调用 `/chat/query`、`/admin/import/*` 和 job status API |

已移除多页入口：`ui/pages/0_Chat.py`、`ui/pages/3_Reflection.py`。Reflection 数据仍可通过 `/reflection/*` API 查看，但不再作为课堂展示 UI 的独立页面。

UI 数据源控制：
- Reddit subreddit 多选，默认 `worldnews`；
- 官方/evidence 来源多选，默认 `ap/reuters/bbc/nyt`；
- Reddit 与官方来源日期范围；
- `Append new data` / `Overwrite retained data` 导入模式；
- overwrite 必须勾选确认，后端也要求 `confirm_overwrite=true`。

建议问题只给模板，不写死具体 topic；用户在 Draft question 中替换 `<your topic>` 或粘贴 claim 后再提问。

启动：`streamlit run ui/streamlit_app.py`

---

## 5. 端到端命令清单

```bash
# ── 准备 ──
psql -d society_db -f db/schema_v2.sql              # 建表
python -m scripts.seed_planner_memory                # Chroma 3 冷启动

# ── 后台采集 ──
python main.py --jsonl tests/fixtures/posts_v2_smoke.jsonl   # 跑社区数据
python main.py --subreddit conspiracy --days 3                # 真 Reddit 抓取
python -m agents.official_ingestion_pipeline --once           # 拉 5 个官方源

# ── 服务 ──
uvicorn api.app:app --reload --port 8000              # API
streamlit run ui/streamlit_app.py --server.port 8501  # UI

# ── 维护 ──
python -m scripts.decay_chroma_experience            # 衰减扫描（建议 cron 每天）
python -m scripts.rebuild_chroma2_schema --dry-run   # Schema 漂移修复
python -m agents.official_ingestion_pipeline --replay --date 2026-05-03
                                                      # 回放 jsonl 到 Chroma 1
python -m scripts.migrate_run_lineage                # 迁移老 PG 加 lineage 列

# ── Run lifecycle（生产硬约束 Day 4）──
python -m scripts.data_admin scan-pending            # 列出未提交的 run
python -m scripts.data_admin rollback RUN_ID         # 删除某个 run 的全部数据
python -m scripts.data_admin rollback-all-pending    # 启动时崩溃恢复
python -m scripts.data_admin show RUN_ID             # 看 manifest

# ── 调度器（一条命令 bootstrap + 守护 daemon）──
python -m scripts.scheduler                           # 守护 + 启动跑一次
python -m scripts.scheduler --bootstrap               # 仅 bootstrap
python -m scripts.scheduler --once --task official_sources

# ── 测试 ──
pytest tests/                                         # 全量单测（142 passed）
PYTEST_RUN_LIVE_SCHEMA=1 pytest tests/test_schema_consistency.py::test_live_schema_consistency
                                                      # 真实 PG + Chroma 一致性
```

---

## 6. 关键不变量（生产硬约束）

1. **NL2SQL 仅 SELECT**：`_sanitise_sql` + 强制 LIMIT + statement_timeout + READ ONLY 事务
2. **Schema 双写**：每次 SchemaProposal 必须同步 PG schema_meta + Chroma 2，三项一致性测试守门
3. **三段式冲突替换**：< 0.92 / 0.92-0.95 / ≥ 0.95，NL2SQLMemory 与 PlannerMemory 共用
4. **错误归因层级**：L1 内部错误自纠不进 Reflection；L2 Critic-visible 才路由
5. **Bounded Planner**：max 3 分支并行 + max 5 总步
6. **Critic retry**：1 次 → 二次失败标 `needs_human_review=True`
7. **Posts 不向量化**：用 topic_id JOIN（语义解析后）+ Kuzu entity 关联 + tsvector 兜底
8. **Anchor 文档不衰减**：`kind=schema` / `kind=module_card` 永不被 decay scanner 删
9. **每次 chat 主路径最多 3 次 LLM**：rewriter + writer + critic（critic 可跳过）；conversation_compactor 是 save 阶段的旁路调用，不计入主路径预算
10. **多分支默认**：Planner 默认 fan-out 到 ≥2 个分支（除 community_count），让 ReportWriter 真正综合多源
11. **Session 长度封顶**：`conversation` 永远不超过 `SESSION_MAX_TURNS`，老内容压缩进 `summary` 滚动保留

---

## 7. 配置要点（`config.py` / `.env`）

| 变量 | 默认 | 用途 |
|---|---|---|
| `OPENAI_API_KEY` | (必填) | LLM + embedding |
| `OPENAI_MODEL` | gpt-4o | rewriter / writer / critic / NL2SQL / topic label |
| `EMBEDDING_MODEL` | text-embedding-3-small | 1536 维 |
| `POSTGRES_DSN` | postgresql://society:society_pass@localhost:5432/society_db | 写连接 |
| `POSTGRES_READONLY_DSN` | (空 → fallback to POSTGRES_DSN) | NL2SQL 专用只读 |
| `NL2SQL_MAX_REPAIR_ROUNDS` | 3 | repair loop 上限 |
| `NL2SQL_RESULT_ROW_LIMIT` | 1000 | SQL LIMIT 强制上限 |
| `NL2SQL_STATEMENT_TIMEOUT_MS` | 5000 | PG statement_timeout |
| `NL2SQL_CONFLICT_SIM_LOW / HIGH` | 0.92 / 0.95 | Chroma 冲突阈值 |
| `EXPERIENCE_TTL_DAYS` | 30 | 经验衰减 |
| `EXPERIENCE_MIN_CONFIDENCE` | 0.2 | 经验衰减底线 |
| `MULTIMODAL_DAILY_BUDGET_USD` | 5.0 | 图像理解日预算 |
| `MULTIMODAL_MIN_LIKES / MIN_REPLIES` | 50 / 20 | 图像理解采样阈值 |
| `CHROMA_OFFICIAL/NL2SQL/PLANNER_COLLECTION` | chroma_official / nl2sql / planner | Collection 名 |
| `SESSION_MAX_TURNS` | 40 | 单会话窗口大小（超过即压缩+截断） |
| `SESSION_MIN_TURNS_TO_COMPACT` | 10 | LLM 压缩最低批量（小 overflow 也按这个量截）|
| `SESSION_SUMMARY_MAX_CHARS` | 1200 | 滚动摘要长度上限 |

---

## 8. 当前已知未实现 / 后续工作

- **propagation_path KG 查询**：Kuzu 里 Replied 边目前几乎没有数据（IngestionAgent 只写 Posted；社区 fixture 没有 reply 链）。需要 Phase 6 补 reply 抓取。
- **Reuters / AP 全文**：用 Google News 代理后只能拿 title，正文取不到。要全文需要专门接 outlet API 或自建爬虫。
- **官方来源历史窗口**：`/admin/import/official` 当前基于 RSS 当前 feed 导入；日期范围会记录在 job 和 UI 查询上下文里，但还不是任意历史 backfill。
- **多用户 / 鉴权**：`/retrieve/*` `/reflection/*` 是裸 API，部署到公网前必须加 auth。
- **BM25 索引**：当前每次 retrieval 在过滤后的 dense 子集上重建，性能在 5K+ chunk 时会退化；需要 Phase 6 预建 BM25 索引。
- **TopicResolver 阈值**：`min_similarity=0.22 / gap_ratio=1.5` 是基于 8-post fixture 调出来的；上量后需要重新校准。

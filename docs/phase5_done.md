# Phase 5 完成总结（redesign-2026-05）

> 状态：Phase 5 全部就位，Redesign-2026-05 主干完成
> 日期：2026-05-01

## 1. 已交付的模块

### 评分衰减扫描器（Phase 5.1）
- `scripts/decay_chroma_experience.py`：扫 Chroma 2/3 → 30 天未命中 + confidence 低于阈值 → 删除；保留 anchor (kind=schema / module_card)
- 配置：`EXPERIENCE_TTL_DAYS=30 / EXPERIENCE_MIN_CONFIDENCE=0.2`
- 用法：cron 每天跑一次 `python -m scripts.decay_chroma_experience`

### Ablation runner 真实化（Phase 5.2）
- `agents/ablation_runner.py`：`AblationRunner(context)`：muted recall + 重跑 planner+writer+critic 验证因果；24h cost cap=3 record/次
- ChatOrchestrator 在每次 Critic 失败时把真实 runner 注入 ReflectionStore（Phase 3 留的占位被替换）

### Reflection 看板（Phase 5.3 + 5.4）
- `api/routes/reflection.py`：`GET /reflection/{chroma2,chroma3,log}` + `DELETE /reflection/{chroma2,chroma3}/{id}`
- `ui/pages/3_Reflection.py`：3 Tab（NL2SQL / Planner / 审计日志）+ 搜索 + 手动删除 + JSON 详情

### v1 残留清理（Phase 5.5）
**删除的文件**（共 30+）：
- `capabilities/` 整目录（7 个能力 + base + __init__）
- v1 agents：`counter_message.py / visual.py / critic.py / risk.py / community.py / analysis.py / report.py / planner.py / router.py / precompute_pipeline.py`
- v1 services：`stable_diffusion_service.py / intervention_decision_service.py / actionability_service.py / x_api_service.py / counter_effect_service.py / monitor_service.py / cli.py / answer_composer.py / chroma_service.py / metrics_service.py`
- v1 tools：`decision_tools.py / visual_tools.py / evidence_tools.py / graph_tools.py / run_query_tools.py`
- v1 models：`claim.py / persuasion.py / risk_assessment.py / immunity.py / counter_effect.py / community.py / report.py`（旧 IncidentReport）
- v1 API：`api/routes/capabilities.py`
- v1 测试：`tests/test_phase{1,2,3}_chat.py / test_functional.py`
- v1 脚本：`scripts/seed_knowledge.py`
- 整片 v1 配置常量：`SD_MODEL_ID / SD_DEVICE / VISUAL_WIDTH / VISUAL_HEIGHT / CRITIC_MAX_RETRIES / COUNTER_VISUALS_DIR / COUNTER_EFFECTS_DB / CHROMA_CLAIMS_COLLECTION / CHROMA_ARTICLES_COLLECTION / CLAIM_EMBED_SIM_HIGH / CLAIM_EMBED_SIM_LOW`

**改造的文件**：
- `models/manifest.py`：v1 字段精简，新增 `schema_version="v2"`
- `agents/knowledge.py`：从 v1 全功能 KnowledgeAgent 砍到只剩 `classify_post_emotions`
- `agents/ingestion.py`：移除 `XApiService` 依赖；`ingest_posts_from_jsonl` 用纯 stdlib JSON
- `services/postgres_service.py`：移除 v1 `save_report(IncidentReport)`
- `services/manifest_service.py`：阈值改为 NL2SQL 三段式
- `services/__init__.py / agents/__init__.py / models/__init__.py`：v2 导出列表
- `main.py`：仅保留 v2 通路
- `ui/components/chat_response.py`：v2 化（branches_used / branch_outputs / citations 渲染）

### 测试 + 验收（Phase 5.6）
- `tests/test_phase5_v2.py` —— 9 个测试覆盖 decay scanner / AblationRunner / 反射 API
- 全量 `pytest tests/` ：**93 passed, 1 skipped**

### 文档
- `docs/phase5_done.md`（本文件）

## 2. 最终目录结构（v2 架构）

```
agents/
  ablation_runner.py            (Phase 5.2)
  chat_orchestrator.py          (Phase 4.5; v2 主入口)
  entity_extractor.py           (Phase 1.4)
  ingestion.py                  (slim)
  knowledge.py                  (slim; emotion-only)
  multimodal_agent.py           (Phase 1.3)
  official_ingestion_pipeline.py (Phase 1.5)
  planner_v2.py                 (Phase 4.2)
  post_dedup.py                 (Phase 2.8)
  precompute_pipeline_v2.py     (Phase 1+2)
  quality_critic.py             (Phase 4.4)
  query_rewriter.py             (Phase 4.1)
  report_writer.py              (Phase 4.3)
  schema_agent.py               (Phase 2.2)
  topic_clusterer.py            (Phase 2.7)

services/
  chroma_collections.py         (Phase 2.3)
  claude_vision_service.py
  embeddings_service.py
  kuzu_service.py               (扩展 v2 关系)
  manifest_service.py
  news_search_service.py
  nl2sql_memory.py              (Phase 2.3 + Phase 3 三段式冲突)
  planner_memory.py             (Phase 3.5)
  postgres_service.py           (扩展 v2 表)
  reddit_service.py
  reflection_store.py           (Phase 3.6 + Phase 5)
  schema_sync.py                (Phase 2.4)
  session_store.py
  telegram_service.py
  whisper_service.py
  wikipedia_service.py

tools/
  hybrid_retrieval.py           (Phase 3.2; Branch A)
  kg_query_tools.py             (Phase 3.4; Branch C)
  nl2sql_tools.py               (Phase 3.3; Branch B)

models/
  branch_output.py / chat.py / entity.py / evidence.py /
  manifest.py / module_card.py / official_chunk.py /
  post.py / query.py / reflection.py / report_v2.py /
  schema_proposal.py / session.py

api/routes/
  artifacts.py / chat.py / reflection.py / retrieve.py / runs.py

scripts/
  decay_chroma_experience.py    (Phase 5.1)
  rebuild_chroma2_schema.py     (Phase 2.4)
  seed_planner_memory.py        (Phase 3.5)
  scheduler.py                  (legacy; v2 仍可用)
```

## 3. 关键不变量

下面这些是 v2 整套链路在生产里必须保持的硬约束：

1. **NL2SQL 仅 SELECT** + 强制 LIMIT + statement_timeout（`tools/nl2sql_tools._sanitise_sql`）
2. **Schema 双写**：每次 SchemaProposal 同时写 PG schema_meta + Chroma 2，然后清孤儿（`services/schema_sync.SchemaSync.apply_proposal`）
3. **三段式冲突替换**：< 0.92 追加 / 0.92-0.95 直接覆盖 / >= 0.95 LLM 仲裁（NL2SQLMemory + PlannerMemory 共用）
4. **错误归因层级**：L1 NL2SQL 内部错误自纠不进 Reflection；L2 Critic-visible 错误进 ReflectionStore
5. **Bounded Planner**：max 3 分支并行 + max 5 总步
6. **Critic retry** 1 次 → 二次失败标 `needs_human_review=True`
7. **Posts 不向量化**：用 topic_id JOIN + Kuzu entity 关联代替 + tsvector + pg_trgm 兜底
8. **Anchor 文档不衰减**：kind=schema / module_card 永远不被 decay scanner 删

## 4. 端到端能跑的命令清单

### 后台
```bash
# 启动 v2 precompute（5 stage + schema_propose + persist_v2）
python main.py --subreddit conspiracy --days 3
python main.py --jsonl tests/fixtures/sample_posts.jsonl

# 拉一次官方源
python -m agents.official_ingestion_pipeline --once

# Cold-start Chroma 3
python -m scripts.seed_planner_memory

# 修复 Chroma 2 schema 漂移
python -m scripts.rebuild_chroma2_schema --dry-run
python -m scripts.rebuild_chroma2_schema

# 衰减扫描（建议每天 cron）
python -m scripts.decay_chroma_experience
```

### 前台
```bash
# API
uvicorn api.app:app --reload --port 8000

# UI
streamlit run ui/streamlit_app.py
# 直接打开 ui/pages/3_Reflection.py 看 Reflection 看板
```

### 测试
```bash
pytest tests/   # 93 passed, 1 skipped
```

## 5. 下一步（不再属于 Redesign-2026-05 范畴）

- 性能压测：HybridRetriever 顶到 5K 官方源 chunk 的延迟分位数
- 鉴权 / 多租户：`/retrieve/*` `/reflection/*` 加 auth
- Reflection 周报：根据 reflection_log 自动出 Markdown
- BM25 索引常驻化（避免每次重建）
- 把 v1 chat_response 渲染逻辑替换成 v2-native（不再走 `_legacy_capability_output`）

Redesign-2026-05 主干交付完毕。

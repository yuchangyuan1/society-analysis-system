# Phase 2 完成总结（redesign-2026-05）

> 状态：Phase 2 代码全部就位，单测覆盖核心契约
> 日期：2026-05-01

## 1. 已交付的模块

### 数据层
- `db/schema_v2.sql` —— posts_v2（核心列 + JSONB extra + simhash + tsvector + pg_trgm）、topics_v2、entities_v2、post_entities_v2、schema_meta、reflection_log
- `services/postgres_service.py` —— 新增 `upsert_post_v2 / upsert_topic_v2 / upsert_entity_v2 / link_post_entity_v2 / upsert_schema_meta / list_schema_meta / list_information_schema_columns / search_posts_fulltext / search_posts_trgm / find_simhash_neighbours`
- `services/kuzu_service.py` —— 扩展三条 v2 关系：`Replied / Liked / HasEntity` + `add_replied / add_liked / add_post_has_entity / get_topic_propagation / get_topic_entities`
- `services/chroma_collections.py` —— Chroma 三库统一封装：`official / nl2sql / planner`
- `config.py` —— 新增 `CHROMA_OFFICIAL_COLLECTION / CHROMA_NL2SQL_COLLECTION / CHROMA_PLANNER_COLLECTION / NL2SQL_CONFLICT_SIM_LOW / NL2SQL_CONFLICT_SIM_HIGH`

### Schema 双写 + 一致性
- `models/schema_proposal.py` —— `ColumnSpec / SchemaProposal`，含 sha256 fingerprint
- `agents/schema_agent.py` —— Schema-aware Agent；固定核心列；LLM 仅描述 extra 字段
- `services/schema_sync.py` —— `apply_proposal()` 双写 PG + Chroma 2（staging swap 删孤儿）；`verify()` 输出 `ConsistencyReport`
- `scripts/rebuild_chroma2_schema.py` —— 重建命令（`--dry-run / --keep-experience / --full-reset`）
- `services/nl2sql_memory.py` —— Chroma 2 读写 + 三段式冲突替换（< 0.92 追加 / 0.92–0.95 直接覆盖 / ≥ 0.95 LLM 仲裁）

### 业务模块
- `agents/topic_clusterer.py` —— 后台 post-level 聚类（KMeans + 主导情绪 + 实体名打标签）
- `agents/post_dedup.py` —— simhash 64-bit + Hamming ≤ 3；长文 pg_trgm 兜底
- `agents/precompute_pipeline_v2.py` —— 新增 `schema_propose / persist_v2` 两个阶段，串入所有 Phase 2 组件
- `main.py` —— `--pipeline v2` 通路接 schema_agent / schema_sync / pg / kuzu

### 测试
- `tests/test_schema_consistency.py` —— PG ↔ schema_meta ↔ Chroma 2 三项一致性测试 + fingerprint drift 测试 + 标记跳过的 live 集成版本
- `tests/test_phase2_v2.py` —— SchemaProposal 指纹稳定性、SchemaAgent 核心列保留、NL2SQLMemory 三段式冲突、PostDeduper、TopicClusterer、Pipeline v2 stage 串联

## 2. 关键契约

### Schema 双写（Phase 2 5b）
1. SchemaAgent 输出 `SchemaProposal`（含 fingerprint）
2. `SchemaSync.apply_proposal` 一次循环里：
   - 逐列写 `schema_meta`（PG 事务）
   - 逐列写 Chroma 2（kind=schema，确定性 id `schema::table::column`）
   - 删除 Chroma 2 中本次没出现的 kind=schema 文档（孤儿清理）
3. `tests/test_schema_consistency.py` 检三件事：
   - PG 列都有 schema_meta 行
   - PG 列都有 Chroma 2 文档
   - Chroma 2 文档没有指向已删除列的孤儿
   - PG ↔ Chroma 2 fingerprint 一致

### NL2SQL 冲突替换（Q11=B）
- < 0.92：追加，不删
- 0.92 ≤ sim < 0.95：直接删旧追加新
- ≥ 0.95：调 `llm_judge(new, old)`；返回 True 才删旧

### Post 去重（7c-F）
- simhash 主路径，Hamming ≤ 3 视为重复
- > 500 token 的长文走 pg_trgm 二次确认（≥ 0.85）
- 不用 embedding：避免与 topic 聚类阶段重复嵌入；项目已决定 posts 不向量化

## 3. 流程图（Phase 2 后的后台 Pipeline）

```
fetch_posts
  -> ingest (multimodal -> entity_extract -> simhash + dedup)
    -> normalize
      -> emotion_baseline
        -> topic_cluster (post.topic_id assigned in-place)
          -> schema_propose (Schema Agent -> double-write PG + Chroma 2)
            -> persist_v2 (PG posts_v2 / topics_v2 / entities_v2 + Kuzu rels)
```

## 4. 已知 / 后续工作

- **Postgres 不可用时的行为**：`schema_propose` 和 `persist_v2` 会记 `error` 但不阻塞前置 stage。CI 跑没有 PG 的环境，stage status 表会有 error，这是预期。
- **Chroma 写入未做 atomic swap**：当前是"新写入 + 删孤儿"的串行；中途崩溃会留下不一致状态。靠 `tests/test_schema_consistency.py` + 重建命令兜底。
- **TopicClusterer 依赖 sklearn**：缺 sklearn 时返回空列表并记日志。`pyproject.toml` 已经依赖 sklearn（验证通过）。
- **kuzu 写入是 best-effort**：单条失败不阻塞批次，错误进 log，不进 manifest。
- **NL2SQL hit_count 更新**：当前用 `cols.nl2sql.handle.update`，假设 chromadb >= 0.4。早期版本不支持，需要测试时 mock 掉。

## 5. 下一步

进入 Phase 3：前台三大检索分支（Evidence Retrieval / NL2SQL / Knowledge Graph Query）+ Hybrid 检索（BM25 + Dense + RRF + Rerank）+ NL2SQL 内部校验循环。

# Phase 3 完成总结（redesign-2026-05）

> 状态：Phase 3 三大检索分支 + Reflection 自动剔除全部就位
> 日期：2026-05-01

## 1. 已交付的模块

### 数据契约（Phase 3.1）
- `models/evidence.py` —— `Citation / EvidenceChunk / EvidenceBundle`
- `models/branch_output.py` —— `EvidenceOutput / SQLOutput / SQLAttempt / KGOutput / KGNode / KGEdge / BranchExecutionStatus`
- `models/module_card.py` —— `ModuleCard / WorkflowExemplar`
- `models/reflection.py` —— `CriticVerdict / ReflectionRecord`（含 layered ErrorKind）

### A. Evidence Retrieval（Phase 3.2）
- `tools/hybrid_retrieval.py` —— `HybridRetriever`：Metadata pre-filter + Dense（Chroma 1）+ BM25（rank_bm25 子集）+ RRF（k=60）+ Rerank（bge-reranker-base 本地，缺包降级）→ `EvidenceBundle`
- 性能上限：top-50 召回 + rerank ≤ 2s

### B. NL2SQL（Phase 3.3）
- `tools/nl2sql_tools.py` —— `NL2SQLTool`：召回 Chroma 2 三类文档 → LLM 生成 SQL → `_sanitise_sql` 白名单 → 只读 DSN 执行 → repair 循环（max 3）→ 失败时写 `kind=error` 经验
- 安全约束：仅 `WITH` / `SELECT`；禁止 `INSERT/UPDATE/DELETE/DROP/...`；禁止多语句；强制 `LIMIT NL2SQL_RESULT_ROW_LIMIT`；`statement_timeout`
- 错误层级：Layer 1（语法 / 列名 / 超时）走内部 repair 循环，**不**写 Reflection；Layer 2（empty_result / limit_hit）才进 Critic
- 配置项：`POSTGRES_READONLY_DSN / NL2SQL_MAX_REPAIR_ROUNDS / NL2SQL_RESULT_ROW_LIMIT / NL2SQL_STATEMENT_TIMEOUT_MS`

### C. Knowledge Graph Query（Phase 3.4）
- `tools/kg_query_tools.py` —— `KGQueryTool`：四类 Cypher 查询
  - `propagation_path(source, target, max_hops)`
  - `key_nodes(topic_id, top_k)`
  - `community_relations(topic_id, min_shared_posts)`
  - `topic_correlation(topic_a, topic_b)`
- 老 `tools/graph_tools.py` 留给 v1 chat 链路；v2 用新文件，保持隔离

### Planner memory + ModuleCard（Phase 3.5）
- `services/planner_memory.py` —— `PlannerMemory`：Chroma 3 读写 + 三段式冲突替换 + `count_branch_combo_successes()`（Q9 置信度规则）
- `SEED_MODULE_CARDS`（3 张）+ `SEED_WORKFLOW_EXEMPLARS`（6 条）
- `scripts/seed_planner_memory.py` —— 启动时 idempotent seed

### Reflection 自动剔除（Phase 3.6 前移）
- `services/reflection_store.py` —— `ReflectionStore`：
  - 错误归因路由：`sql_empty_result → Chroma 2`；`missing_branch / wrong_branch_combo / off_topic → Chroma 3 workflow_error`；`citation_missing / numeric_mismatch → Chroma 3 composition_error`
  - Ablation 验证因果（cost cap = 3 record / 次）
  - 24h 反震荡（同 record 删>2 次进 quarantine）
  - PG `reflection_log` 审计表

### API（Phase 3.7）
- `api/routes/retrieve.py` —— `POST /retrieve/{evidence,nl2sql,kg}`，三分支独立可调
- `api/app.py` 注入 retrieve router

### 测试（Phase 3.8）
- `tests/test_phase3_v2.py` ——
  - HybridRetriever：BM25 缺失降级 / RRF 融合 / 空 query 处理 / BM25 子集打分
  - `_sanitise_sql`：拒非 SELECT / 拒多语句 / 加 LIMIT / 截 LIMIT / 接受 WITH CTE
  - NL2SQLTool：成功路径 / repair 用尽后写 error / empty_result 标记
  - KGQueryTool：四类查询 + Kuzu 缺失降级
  - ModuleCard / PlannerMemory：doc 文本 / branch combo count
  - ReflectionStore：路由规则 / Ablation / 反震荡 / passed verdict 仅审计

## 2. 关键契约（Phase 3 新增）

### 错误归因层级（PROJECT_REDESIGN_V2.md 7b-(3)）

| 层 | 错误 | 处理 |
|---|---|---|
| 1 (NL2SQL 内部) | `sql_syntax / sql_unknown_column / sql_type_mismatch / sql_timeout / sql_connection / sql_other` | repair loop 自纠；3 轮失败才写 Chroma 2 `kind=error`，**不**经 Reflection |
| 2 (Critic) | `sql_empty_result / sql_limit_hit / missing_branch / wrong_branch_combo / citation_missing / numeric_mismatch / off_topic` | `ReflectionStore` 路由到 Chroma 2 / Chroma 3 |

### Q9 置信度规则
Planner 决定是否调 Chroma 3 few-shot：`PlannerMemory.count_branch_combo_successes(branches)` >= 3 视为高置信，否则查 few-shot；Phase 4 Planner 接入这个 hook。

## 3. 流程图（Phase 3 后的前台 Pipeline）

```
User question
  -> [Phase 4] Query Rewriter (待写)
    -> [Phase 4] Router         (待写)
      ├─► tools/hybrid_retrieval.py     (Branch A; Phase 3 已就位)
      ├─► tools/nl2sql_tools.py         (Branch B; Phase 3 已就位)
      └─► tools/kg_query_tools.py       (Branch C; Phase 3 已就位)
    -> [Phase 4] Planner -> Report Writer -> Critic
                                          └─► ReflectionStore (Phase 3 已就位)
```

## 4. 已知 / 后续工作

- **HybridRetriever 的 BM25 corpus**：当前从 dense 召回结果中重建索引；当 dense 召回为空时 BM25 也为空。若官方源有大量低 cosine 的强关键词命中需求，Phase 4 要把 BM25 corpus 提升到 metadata 过滤后的大集合（潜在性能问题，需要离线 BM25 索引）。
- **Reranker 模型加载**：首次调用会下载几百 MB 模型；CI / 单机测试请考虑预热或 mock。
- **Kuzu 类型签名**：`_safe_execute` 当前是 v1 内部方法（私有约定）。Phase 4 把 KGQueryTool 直接挪到 KuzuService 上层时再处理可见性。
- **NL2SQL READ-ONLY**：当前 `_execute` 用 `SET TRANSACTION READ ONLY`；生产强烈建议配独立的 `POSTGRES_READONLY_DSN`（角色权限只 SELECT）。
- **/retrieve API 没有鉴权**：调试用，部署到公网前必须加。
- **Reflection 的 Ablation runner 是 noop 占位**：Phase 5 接 Critic 实例后注入真的"重跑不带这条 record"逻辑。

## 5. 下一步

进入 Phase 4：Query Rewriter / Bounded Planner 重写 / Report Writer / Quality Critic / Chat Orchestrator 接新链路 / 旧 capabilities 测试统一迁移。

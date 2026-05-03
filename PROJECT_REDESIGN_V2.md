# Society Analysis 项目改造方案 v2

> **状态**：✅ 全部 5 个原始 Phase 交付，并完成 KG 改造 4 阶段（Phase A→D）+ 8 天生产化
> **日期**：2026-05-03
> **当前测试**：142 passed / 1 skipped
> **后续文档**：
>   - `docs/kg_redesign_plan.md` — KG 模块改造方案（Phase A→D 已全部落地）
>   - `docs/review.md` — 生产化前 8 类问题清单 + 拍板记录（除 #3/#6/#7 外全部完成）
>   - `workflow.md` — 当前架构与模块清单
>   - `README.md` — 上手 + 运维命令
> **依据**：`ChatGPT Image 2026年5月1日 11_34_49.png` 流程图
> **替代**：当前 `complete_project_transformation_plan.md` 中尚未落地的部分

本文档基于流程图重新设计整个系统。当前实现（24 stage 后台 + 7 Capability 前台）会被显著改造。详细差异和分阶段路线见下文。

---

## 0. 改造目标（一句话）

把项目从"批处理裁决型"（后台算好 claim 5 档裁决 + 反制图卡，前台只翻译）改造成"实时检索综合型"（后台只做基础数据沉淀到三库，前台按问题动态编排 RAG/SQL/KG 三路检索 + 报告综合 + 自我校验）。

---

## 1. 目标架构总览

### 1.1 后台 Pipeline（定时执行 / 每日自动）

```
┌─────────────────────────── A. 社区数据臂 ───────────────────────────┐
│                                                                       │
│  Reddit ──► 帖子抓取 ──► 清洗/去重/压缩 ──► 图片理解（多模态）         │
│                                              │                        │
│                                              ▼                        │
│                                       Topic 聚类 / Entity 抽取 /       │
│                                       情感分析                         │
│                                              │                        │
└──────────────────────────────────────────────┼────────────────────────┘
                                               │
┌─────────────────────────── B. 官方源臂 ──────┼────────────────────────┐
│                                              │                        │
│  权威站点（BBC / NYT 等） ──► 清洗去噪 ──► Chunk 切分 ──► Metadata 添加│
│                                              │                        │
└──────────────────────────────────────────────┼────────────────────────┘
                                               │
                                               ▼
                            ┌─────────────────────────────────┐
                            │  三路并行写入                    │
                            ├─────────────────────────────────┤
                            │  a. Kuzu 图谱（图数据）         │
                            │      User/Post/Topic/Entity      │
                            │      POSTED/REPLIED/MENTIONS...  │
                            │                                  │
                            │  b. PostgreSQL（结构化）        │
                            │      核心列固定 + JSONB 扩展     │
                            │      （Schema-aware Agent 写入） │
                            │                                  │
                            │  c. Chroma 1（向量）  ->  用于判断某个话题帖子内容和官方来源对比的真假，谣言鉴别，并给出证据，或者说明缺失官方来源的信息无法判断            │
                            │     只存放官方权威来源的信息（例如New York Times, BBC, 维基百科等等），不存放posts!│
                            |  
                               d. Chroma 2 (向量)  ->   给NL2SQL功能使用，确保根据具体的任务编写出正确的SQL语句进行PostgreSQL数据库的增删改查
                                 存放PostgreSQL数据库存储中的表名和描述、字段名和描述、NL2SQL的正确示范、NL2SQL使用过程中纠正错误形成的一套经验方法论（动态更新）

                               e. Chroma 3 (向量)  ->   给planner Agent使用，确保工作流编排和工具调用稳定，不出现幻觉
                                 存放知识图谱、postgreSQL、RAG 等 planeer可以调用的模块的功能描述和调用描述、正确的问题请求与对应工作流编排的示范对、planner在reflection过程中解决问题形成的一套工作流编排和工具调用的方法论（在reflection的过程中动态更新）
                            └─────────────────────────────────┘
```

### 1.2 前台 Pipeline（实时交互）

```
1. 用户提问
   │
2. Query Rewrite / 子任务拆分 / 上下文校验
   │
3. Router（意图识别 + 子任务分发）
   │
   ├──► A. Evidence Retrieval (chroma 1)
   │       Metadata 过滤 → Dense + BM25 → RRF 融合 → Rerank → Citation
   │
   ├──► B. NL2SQL
   │       NL → SQL → Postgres CRUD → 校验 → 错误自纠
   │       ⇆ NL2SQL 经验 RAG（few-shot 样例 / 反例）
   │
   └──► C. Knowledge Graph Query (Kuzu)
           传播路径 / 关键节点 / 社区关系 / 话题关联
   │
4. Planner（自动编排 workflow，决定哪些分支并行调度）
   │
5. 报告编写 Agent（汇总 RAG 、 SQL 、 KG  其中planner 调用了的模块的输出）
   │
6. 报告质量校验 Agent（citation 完整性 / 数字一致 / 反幻觉）
   │
7. 输出：自然语言回答 + 引用 + 结构化数据
   │
   ⇄ Reflection / 结果校验
       错误回写到经验库(chroma 3)，下次更聪明
```

### 1.3 共享数据存储

| 存储 | 用途 | 写入者 | 读取者 |
|---|---|---|---|
| **PostgreSQL** | 帖子结构化数据（核心列（可以包括发帖人、发帖时间、压缩后的帖子内容、所属话题Topic、情绪等） + JSONB 扩展字段） | 后台 Schema-aware Agent | 通过 NL2SQL 进行增删查改 |
| **Kuzu** | 用户-帖子-话题-实体的图关系 | 后台 Kuzu writer | KG Query 分支 |
| **Chroma 1** | ->  用于判断某个话题帖子内容和官方来源对比的真假，谣言鉴别，并给出证据，或者说明缺失官方来源的信息无法判断       |只存放官方权威来源的信息（例如New York Times, BBC, 维基百科等等），不存放posts!│
| **Chroma 2**|  ->   给NL2SQL功能使用，确保根据具体的任务编写出正确的SQL语句进行PostgreSQL数据库的增删改查改                     |存放PostgreSQL数据库存储中的表名和描述、字段名和描述、NL2SQL的正确示范、NL2SQL使用过程中纠正错误形成的一套经验方法论（动态更新）
| **Chroma 3**|  ->   给planner Agent使用，确保工作流编排和工具调用稳定，不出现幻觉                                               |存放知识图谱、postgreSQL、RAG 等 planeer可以调用的模块的功能描述和调用描述、正确的问题请求与对应工作流编排的示范对、planner在reflection过程中解决问题形成的一套工作流编排和工具调用的方法论（在reflection的过程中动态更新）|

---

## 2. 当前 vs 目标 — 差异对照

| 方面 | 当前实现 | 目标设计 | 改动级别 |
|---|---|---|---|
| **后台主干** | 24 stage（含 claim 抽取/裁决/反制/出图） | 极简 5 步（抓 → 清 → 多模态 → 分析 → 入三库） | **大改** |
| **图片理解** | 无 | 后台多模态 → 文字描述并入 post 流 | **新增** |
| **官方源** | 在线按 claim 即时查 Wikipedia/NewsSearch | 后台定期入库到 Chroma 作为基线证据库 | **大改** |
| **Postgres Schema** | 固定 `db/schema.sql` | 核心列固定 + JSONB 扩展（Schema-aware Agent 决策） | **新增** |
| **Claim 5 档裁决** | 后台批量算好 | 删除：改为查询时 RAG 综合判断 | **移除** |
| **干预决策 + 反制文案 + 图卡** | 后台 18-21 stage | 删除（图中未出现） | **移除/降级** |
| **社区检测 / 角色分类 / 桥接指标** | 后台批量（13-16 stage） | 后置：查询时由 Kuzu Cypher 即席算 | **后置** |
| **前台编排** | Router → Planner（7 固定模板）→ 7 Capability | Query Rewrite → Router → 三大分支 + Planner → 报告 → 校验 → Reflection | **大改** |
| **Query Rewrite** | 无 | 显式独立步骤 | **新增** |
| **NL2SQL 分支** | 无 | 完整分支 + 动态更新的参考加经验 RAG (chroma 2) + 错误自纠 | **新增** |
| **Hybrid 检索** | 仅 Chroma 单路召回 | Metadata + Dense + BM25 + RRF + Rerank | **新增** |
| **报告编写 Agent** | AnswerComposer（仅翻译） | 独立 Agent，汇总三源做综合 | **升级** |
| **质量校验** | 无（仅后台 critic 看反制文案） | 前台独立 Agent 校验最终回答 | **新增** |
| **Reflection 闭环** | 无 | 错误回写经验库(chroma 3) | **新增** |
| **Capability 抽象** | 7 个核心抽象 | 删除：Planner 直调三大分支 + Tool | **重构** |

---

## 3. 分阶段实施路线（建议总周期 4-6 周）

### Phase 1 — 后台瘦身 + 多模态采集（约 1 周）

**目标**：把 24 stage 砍成 8 步，符合图中后台流。、

**保留**：
- `fetch_posts` → `ingest` → `normalize` → `emotion_baseline` → `topic_cluster`

**新增**：
- `agents/multimodal_agent.py` —— 用现有 `services/claude_vision_service.py` 把帖子里的图片转文字描述，拼回 post 文本流
- `agents/entity_extractor.py` —— 抽取人物/组织/地点 entity（替代复杂的 claim 抽取链）
- `agents/official_ingestion_pipeline.py` —— 独立的官方源采集臂：站点抓 → 清洗 → 按 token 数 chunk 切分 → metadata 标注（来源/时间/作者/可信度 tier）→ 写 Chroma `articles` collection

**删除**（或先标 deprecated）：
- `agents/counter_message.py` / `agents/visual.py` / `agents/critic.py`
- `services/stable_diffusion_service.py` / `services/intervention_decision_service.py` / `services/actionability_service.py`
- 后台 claim_extract / actionability / evidence_gather / stance_score / tier_classify / claim_verdict / community_detect / account_roles / bridge_influence / propagation_summary / coordinated_detect / intervention_decide / counter_message / critic_review / visual_cards 阶段

**风险点**：
- `IncidentReport`、`Claim`、`InterventionDecision` 模型被多处 import；删除前需先用 grep 摸清依赖
- 现有 37 个 chat 测试会全红，需在 Phase 4 末重写

**交付物**：
- 新 `agents/precompute_pipeline_v2.py`（暂时与旧版并存）
- 多模态 + 实体抽取的单元测试

---

### Phase 2 — Schema-aware 入库 + 三库统一写入（约 1 周）

**目标**：实现图中 5a/5b/6 三路写入。

**5a. Kuzu 图谱扩展**：
- 改 `services/kuzu_service.py`：节点 = `User`/`Post`/`Topic`/`Entity`；关系 = `POSTED`/`REPLIED`/`LIKED`/`MENTIONS`/`BELONGS_TO_TOPIC`/`HAS_ENTITY`
- 后台批量写入 Cypher

**5b. Schema-aware Agent**（推荐保守做法）：
- 核心列固定：`post_id` / `author` / `text` / `ts` / `subreddit` / `topic_id` / `dominant_emotion`
- 动态字段统一塞 JSONB 列 `extra: jsonb`
- 新增 `agents/schema_agent.py`：每次 run 启动看一批样本 post，输出 `SchemaProposal`（建议哪些字段进 `extra`）
- **不**做真实 ALTER TABLE（避免 schema 漂移噩梦）

**6. Embedding（三个独立 Chroma collection）**：
- **不**重新引回 posts collection —— 帖子只走 Postgres + Kuzu，不向量化
- `chroma_official` collection（= Chroma 1）—— 官方源 chunk
- `chroma_nl2sql` collection（= Chroma 2）—— Schema 描述 + 正确示范 + 错误经验
- `chroma_planner` collection（= Chroma 3）—— 模块功能描述 + 编排示范 + 失败教训

**Schema-aware Agent → Chroma 2 的同步契约**（关键，已确认）：
- Schema Agent 每次 run 输出 `SchemaProposal` 时，必须把 `(table_name, column_name, description, sample_values)` 同步写入 Chroma 2
- 这样 NL2SQL 在生成 SQL 时按相似度召回相关字段描述，避免幻觉表名/列名
- 这一步是 NL2SQL 分支能正常工作的前提

**双写一致性保证**（已确认采纳）：
- **事务封装**：Schema Agent 在一次提交里同时写 PG 和 Chroma 2；任一失败 → 整体回滚（Chroma 2 写入用 staging collection + atomic swap，避免 Chroma 没有原生事务）
- **Schema 指纹**：每次 schema 变更生成 `schema_fingerprint = sha256(sorted(table.column.type))`，PG 一份（`schema_meta` 表）、Chroma 2 一份（每条 schema 文档的 metadata）。指纹不一致即为漂移
- **一致性测试**（pytest `tests/test_schema_consistency.py`）：
  - `test_pg_chroma2_fingerprint_match`：从 `information_schema.columns` 拉 PG 实际 schema，对比 Chroma 2 里所有 `kind=schema` 文档的指纹
  - `test_every_pg_column_has_chroma_doc`：PG 每个 (table, column) 在 Chroma 2 都能召回到对应描述
  - `test_no_orphan_chroma_docs`：Chroma 2 里没有指向已删除列的描述（垃圾文档）
  - 三个测试都进 CI 必跑环节，且每次后台 run 结束时自动跑一遍并写入运行日志
- **重建命令**（CLI）：`python scripts/rebuild_chroma2_schema.py`
  - 用途：从 PG `information_schema` + 每列样本值反向重建 Chroma 2 的 schema 部分（不动 success / error 部分）
  - 触发场景：一致性测试失败 / Chroma 2 损坏 / Schema Agent 历史输出有错 / 手动迁移
  - 选项：`--dry-run`（只打印 diff）、`--keep-experience`（默认；只重建 kind=schema）、`--full-reset`（清空整个 Chroma 2）
  - 重建后自动跑一致性测试验证
- **监控**：每次 NL2SQL 调用记录"是否召回到目标表的 schema"，连续 3 次召回为空 → 触发告警 + 自动建议运行重建命令

**Chroma 3 冷启动 seed**：
- 把"NL2SQL 分支能做什么 / Evidence Retrieval 分支能做什么 / KG Query 分支能做什么"的功能描述随代码一起 seed 到 Chroma 3
- 提供 ≥ 30 条"问题 → 工作流"示范对作为初始 few-shot

**交付物**：
- 三库（PG / Kuzu / 三个 Chroma collection）都有 v2 数据；可用 SQL / Cypher / 相似度查询验证一致性
- `db/schema_v2.sql`：核心列 + `extra jsonb` + 索引
- **Postgres 全文检索补偿方案**（因 posts 不向量化）：核心列 `text` 加 `tsvector` GIN 索引 + `pg_trgm` 模糊匹配，作为 NL2SQL 内部的 LIKE/相似度搜索后端

---

### Phase 3 — 前台三大检索分支（约 1.5 周）

**目标**：把现在 5 个分散 tool 文件重组成图中三大分支。

**A. Evidence Retrieval**（替换 `tools/evidence_tools.py`）：
- 新增 `tools/hybrid_retrieval.py`：
  - Metadata pre-filter（来源、时间、可信度 tier）
  - Dense（Chroma cosine）+ BM25（用 `rank_bm25` 库）双路召回
  - RRF（Reciprocal Rank Fusion）融合
  - Rerank（先用 `bge-reranker-base` 本地模型；备选 cohere rerank API）
  - 输出含 Citation 的 `EvidenceBundle` Pydantic
- 性能目标：top-50 召回 + rerank → 总延迟 ≤ 2s

**B. NL2SQL**（全新）：
- 新增 `tools/nl2sql_tools.py`：
  - `generate_sql(nl_query, schema_context, examples, error_lessons)` —— LLM 生成 SQL
    - `schema_context` 来自 Chroma 2 按 NL 召回的相关 (table, column, description)
    - `examples` 来自 Chroma 2 按 NL 召回的成功 (NL, SQL) 对
    - `error_lessons` 来自 Chroma 2 按 NL 召回的相关错误经验
  - `execute_and_validate(sql)` —— 在 Postgres 跑（**只读连接**），返回行 + schema 校验
  - `repair_sql(sql, error)` —— 错误自纠循环（max 3 轮）
- 新增 `services/nl2sql_memory.py`（Chroma 2 读写封装）：
  - 成功的 (NL, SQL) 对 → 写 Chroma 2，metadata 标 `kind=success`
  - 常见错误模式 → 写 Chroma 2，metadata 标 `kind=error`
  - Schema 描述 → 写 Chroma 2，metadata 标 `kind=schema`
  - 查询时按 metadata 过滤 + 相似度召回 top-K few-shot
- **安全约束**：
  - 强制只读 Postgres 用户
  - SQL 白名单：只允许 `SELECT`
  - 强制 `LIMIT 1000`、`statement_timeout = 5s`
- **针对 posts 内容的全文搜索**：NL2SQL 通过 Postgres `tsvector` 全文索引 + `pg_trgm` 实现"主题相关帖子"类查询，不依赖向量召回

**C. Knowledge Graph Query**（升级 `tools/graph_tools.py`）：
- 切到 Kuzu Cypher（`complete_project_transformation_plan.md` 已规划）
- 提供 4 类查询模板：
  - 传播路径（最短路径 / k-hop）
  - 关键节点（PageRank / betweenness）
  - 社区关系（社区检测 + 跨社区桥）
  - 话题关联（共现 / 关联强度）

**交付物**：
- 三个分支独立可调（API：`POST /retrieve/{evidence,nl2sql,kg}`）
- 每个分支带 ≥ 5 个端到端测试

---

### Phase 4 — Query Rewrite + Planner + 报告 Agent + 校验（约 1 周）

**目标**：实装图中前台 1→7 的完整链路。

**新增 `agents/query_rewriter.py`**：
- 子任务拆分（"对比 A 和 B 的传播差异" → 拆成两个独立子任务）
- 上下文校验（合并会话历史、消歧代词）
- 输出 `RewrittenQuery` Pydantic（含 `subtasks: list[Subtask]`）

**重写 `agents/planner.py`**：
- 不再是 7 固定 intent → 模板的硬映射
- 改为：根据子任务类型决定调哪些分支（A/B/C 任意组合并行）
- **仍保持 bounded**：最多 3 分支并行 + 最多 5 步串联
- 输出 `PlanExecution`（含每个分支的输出 + 错误）

**新增 `agents/report_writer.py`**（替换 `services/answer_composer.py`）：
- 汇总 RAG + SQL + KG 三源结果
- 生成结构化 markdown 报告（带 citation）
- 比 AnswerComposer 重，允许做综合推理（一次 LLM 调用，给 system prompt 强约束）

**新增 `agents/quality_critic.py`**：
- 校验报告：
  - Citation 完整性（每个事实声明都有引用？）
  - 数字一致性（报告里的数字和 SQL 结果对得上？）
  - 是否回答了原问题？
  - 有无明显幻觉？
- 失败 → retry（最多 1 次）；二次失败 → 输出降级回答 + 标记 `needs_human_review`

**更新 `agents/chat_orchestrator.py`** 串起整条链路。

**重写测试**：从原 37 个 chat 测试迁移到新链路；保留覆盖率不降。

**交付物**：
- 端到端可工作的新前台链路
- 三栏 UI 轻微调整（Capability 列变成 Branch 列：RAG/SQL/KG）

---

### Phase 5 — Reflection 知识闭环（约 0.5 周）

**目标**：图中右下角的反思回路 —— 错误经验**双轨**回写到 Chroma 2 / Chroma 3。

- 新增 `services/reflection_store.py`：
  - 总入口；负责把 Critic 拒绝事件做**错误归因**，分发到不同的 Chroma 经验库
  - 同时写到 Postgres 表 `reflection_log` 作审计存档
- **错误归因路由**（核心契约）：

  | 错误类型 | 触发条件 | 写入 |
  |---|---|---|
  | NL2SQL 错误 | SQL 执行失败 / 列名表名错 / 结果空但应有 | Chroma 2（kind=error） |
  | NL2SQL 成功 | Critic 通过且本轮用了 NL2SQL 分支 | Chroma 2（kind=success） |
  | Schema 漂移 | NL2SQL 找不到字段 / 描述过期 | Chroma 2（kind=schema，覆盖旧条目） |
  | Planner 编排错误 | 选错分支组合 / 漏调必要分支 / Critic 多次拒绝 | Chroma 3（kind=workflow_error） |
  | Planner 编排成功 | Critic 一次通过 | Chroma 3（kind=workflow_success） |
  | Citation 缺失 | Critic 检测到无引用断言 | Chroma 3（kind=composition_error） |
  | Evidence Retrieval 召回为空 | 官方源没覆盖 | 不写经验库；写运营告警（提示扩官方源采集） |

- Query Rewrite / NL2SQL / Report Writer / Planner 启动时各自召回相关教训作为 negative few-shot
- 加 **TTL（30 天）** 和 **人工审核入口**（避免错误经验越攒越偏）
- **冲突处理**：同一类错误若被反复回写，做去重 + 频次累计，避免 Chroma 经验库被同质化记录灌爆

**交付物**：
- Reflection 表 + API inspector（`/reflection/*`）。课堂展示 UI 已收敛到单页 `ui/streamlit_app.py`，不再保留独立 Reflection 页面。

---

## 4. 模块改动清单速查

### 删除 / 标记 deprecated
- `agents/counter_message.py`
- `agents/visual.py`
- `agents/critic.py`（前台用 `quality_critic.py` 替代）
- `services/stable_diffusion_service.py`
- `services/intervention_decision_service.py`
- `services/actionability_service.py`
- `capabilities/` 整个目录（功能拆到三大分支 + Planner）
- `tools/decision_tools.py`、`tools/visual_tools.py`

### 新增
- `agents/multimodal_agent.py`
- `agents/entity_extractor.py`
- `agents/official_ingestion_pipeline.py`
- `agents/schema_agent.py`
- `agents/precompute_pipeline_v2.py`
- `agents/query_rewriter.py`
- `agents/report_writer.py`
- `agents/quality_critic.py`
- `tools/hybrid_retrieval.py`
- `tools/nl2sql_tools.py`
- `services/nl2sql_memory.py`
- `services/reflection_store.py`
- `db/schema_v2.sql`
- `ui/streamlit_app.py`（单页 Chat + 数据源控制 + 导入按钮 + 路由模块/KG 可视化展示）
- `scripts/rebuild_chroma2_schema.py`（Schema 双写重建命令）
- `tests/test_schema_consistency.py`（PG ↔ Chroma 2 三项一致性测试）

### 重写
- `agents/planner.py`（从固定模板改为 bounded 自由编排）
- `agents/chat_orchestrator.py`（接新链路）
- `agents/precompute_pipeline.py`（瘦身 24 → 8 stage）
- `tools/graph_tools.py`（NetworkX → Kuzu Cypher）
- `services/kuzu_service.py`（扩展节点和关系类型）
- `services/postgres_service.py`（加 JSONB extra 列支持）

### 保留不动
- `services/chroma_service.py`（仅扩 collection，不改实现）
- `services/embeddings_service.py`
- `services/wikipedia_service.py` / `services/news_search_service.py`（移到官方源采集臂内部使用）
- `services/reddit_service.py`
- `models/session.py` / `models/chat.py`
- `api/routes/chat.py` / `api/routes/runs.py`（部分签名调整）
- `ui/streamlit_app.py` 主体（单页 Chat 展示三路路由、KG 图谱和数据源控制）

---

## 5. 数据契约变更

### 删除
- `models/claim.py::Claim` / `ClaimEvidence`
- `models/report.py::IncidentReport.claims`、`InterventionDecision`、`CounterMessage`

### 新增
- `models/post_record.py::PostRecord`（核心列 + JSONB extra）
- `models/entity.py::Entity`
- `models/evidence.py::EvidenceBundle / EvidenceChunk / Citation`
- `models/query.py::RewrittenQuery / Subtask`
- `models/branch_output.py::EvidenceOutput / SQLOutput / KGOutput`
- `models/report_v2.py::Report`（替换 IncidentReport，含 citation 列表 + 分支输出汇总）
- `models/reflection.py::ReflectionRecord`

### 保留
- `models/session.py` / `models/chat.py`（轻微调整：`capability_used` 改为 `branches_used: list[str]`）

---

## 5b. 三个 Chroma 库的契约速查（关键）

由于本方案把 Chroma 拆成 3 个独立用途的 collection，下表是它们的"身份证"：

| 维度 | Chroma 1（官方源） | Chroma 2（NL2SQL 经验） | Chroma 3（Planner 经验） |
|---|---|---|---|
| **服务对象** | Evidence Retrieval 分支 | NL2SQL 分支 | Planner / Report Writer |
| **存储内容** | 官方权威站点 chunk + metadata | Schema 描述 + 成功 (NL, SQL) 对 + 错误经验 | 模块功能描述 + 问题→工作流示范对 + 编排失败教训 |
| **写入者** | 后台官方源采集臂 | 后台 Schema-aware Agent（schema 部分）+ 前台 Reflection（经验部分） | 代码 seed（描述部分）+ 前台 Reflection（经验部分） |
| **读取者** | Hybrid Retrieval（Dense + BM25 + RRF + Rerank） | NL2SQL 生成 / 自纠 | Planner 编排 / Report Writer 综合 |
| **冷启动** | 必须先采集 ≥ 5 个白名单站，至少 1000 篇文档 | Schema Agent 跑过一次 + 30 条人工/LLM 种子 (NL, SQL) | 30 条问题→工作流示范对 + 三大分支功能描述卡 |
| **更新频率** | 后台日更 | 实时（每次 NL2SQL 成功/失败都写） | 实时（每次 Critic 判定后写） |
| **是否做 Rerank** | ✓（高精度需求） | ✗（few-shot 不需要） | ✗（few-shot 不需要） |
| **Metadata 关键字段** | source / publish_date / tier / topic | kind ∈ {schema, success, error} / table_name | kind ∈ {workflow_success, workflow_error, module_card, composition_error} / branches_used |
| **TTL** | 永久（除非源被取消） | error: 30 天；success/schema: 永久 | error: 30 天；success/module_card: 永久 |

### Posts 不向量化的影响和补偿（已根据 Q7 决策更新）

**主路径**："找类似的帖子" = 找**同 topic** 的帖子（topic 聚类用 embedding 模型在后台跑过一次，结果作为 `topic_id` 写进 Postgres 行 + Kuzu 节点）：
- **NL2SQL 路径**：`SELECT * FROM posts WHERE topic_id = X`（直接走 PG 索引）
- **Kuzu 路径**：从 topic 节点出发遍历相关 entity，再回查 entity 关联的其他 posts（跨 topic 但同实体）

**辅助路径**（只在主路径失败时用）：
- Postgres `tsvector` 全文索引 + `pg_trgm` 模糊匹配，给关键词/词形匹配兜底

**Post 去重方案**（我替你拍板：**simhash 主 + pg_trgm 兜底**）：
- 入库时计算 64-bit `simhash`，写到 PG `posts.simhash bigint` 列 + GIN 索引
- Hamming distance ≤ 3 视为重复（推荐工业值；Reddit 使用类似阈值）
- 长帖子（> 500 token）用 pg_trgm `similarity()` 做二次确认（simhash 对长文本退化）
- **不用 embedding**：理由 1）入库吞吐重要，simhash 比 embedding 快 100×；2）embedding 已经在 topic 聚类阶段算过了，不用为去重再算一次；3）项目已决定 posts 不向量化
- **不用 pg_trgm 做主路径**：长文本 trigram 匹配会爆索引

**前提与监控**：
- 主路径质量完全依赖 topic 聚类质量。监控要求：
  - 单 topic 帖子数 < 5 → 标记为 "sparse_topic"，前台查询时自动 fallback 到 entity-graph 路径
  - Topic 总数和分布写入每次 run 的 metrics，异常告警
- 后台 topic_cluster 必须保留 embedding 模型，且和官方源用同一个 embedding model（保证未来跨库语义对齐）

---

## 5c. Router vs Planner 职责边界

修改后的链路里，Router 和 Planner 都做"分发"，容易混淆。明确边界如下：

| 角色 | 输入 | 输出 | 用什么决策 | 何时调用 |
|---|---|---|---|---|
| **Router** | 一条 rewritten subtask | `BranchSet`（要不要调 RAG / SQL / KG，每个 branch 的初步参数） | 规则 + 关键词；轻量 LLM 兜底 | 每个 subtask 各调一次 |
| **Planner** | 多个 subtask 的 BranchSet 集合 | `Workflow`（执行顺序、并行度、步骤间的数据依赖、停止条件） | LLM + 召回 Chroma 3 的示范对 | 整个用户提问调一次 |

简而言之：**Router 决定"调谁"，Planner 决定"怎么调"**。Planner 是 Chroma 3 唯一的读取者；Router 不查 Chroma 3。

---

## 5d. 一次完整调用的数据流示意（端到端）

以"BBC 报道的某事件，在 Reddit 上是怎么被讨论的，谁在带节奏？"为例：

```
1. Query Rewriter 拆出 3 个子任务：
   ① 找官方源里关于该事件的报道
   ② 在 Postgres 找讨论该事件的帖子分布
   ③ 在 Kuzu 找传播路径 / 关键账户

2. Router 各 subtask 决定 branch：
   ① → Evidence Retrieval (Chroma 1)
   ② → NL2SQL (Postgres + Chroma 2 召回 schema/示范)
   ③ → KG Query (Kuzu)

3. Planner（查 Chroma 3 召回示范："官方+社区+图谱"组合的成功案例）
   决定并行执行三个 branch，最终汇总到 Report Writer

4. 三 branch 并行：
   ① Hybrid 检索 → top-5 chunks + citation
   ② NL2SQL：召回 Chroma 2 schema → 生成 SQL → Postgres 跑 → 行结果
   ③ Kuzu Cypher：传播路径 + PageRank top-10 账户

5. Report Writer 综合三源 → markdown 报告 + citation 列表

6. Critic 校验：
   - 每个数字都对得上来源吗？
   - Citation 列表完整吗？
   - 真的回答了"谁在带节奏"这一问吗？

7a. 若通过 → 写 Chroma 2（这条 NL2SQL 成功）+ Chroma 3（这条编排成功）→ 返回用户
7b. 若失败 → 错误归因 → 路由到 Chroma 2 或 Chroma 3 的 error → retry 或降级
```

这条链路把所有部件用一个具体例子串起来，方便后续对照 Phase 4 的实现。

---

## 6. 开放决策点（需要拍板）

下面 6 个问题答案不同，会显著影响实施细节。请逐项给出意见。

### Q1：干预决策 + 反制文案 + 图卡是完全删除还是保留为可选 plugin？
- **方案 A**：完全删除（图中未出现）


### Q2：Postgres 动态 schema 用 JSONB 兜底还是真 ALTER TABLE？
- **方案 A**（推荐）：核心列固定 + JSONB extra 列。零迁移痛苦

### Q3：官方源采集范围？
- **方案 A**（推荐起步）：白名单 5 个站（BBC / NYT / Reuters / AP / Xinhua）

### Q4：NL2SQL 经验 RAG 冷启动语料？
- **方案 C**：用 LLM 自动生成种子，人工审核

### Q5：现有 7 Capability 抽象层是否保留？
- **方案 A**（推荐）：完全删除 capabilities/，Planner 直调三大分支 + Tool。少一层间接

### Q6：多模态成本控制策略？
- **方案 B**（推荐）：只对高互动帖子做（top-N by score / comments）

### Q7（新）：Posts 不向量化后，"语义找相似帖子"怎么办？
posts一般是根据给出的topics进行查找，而不是语义，可以在postgresql中筛选与要查找的post同类topic的其它记录，也可以在kuzu中查找与对应topic相关的其它entity。

### Q8（新）：Chroma 2 / Chroma 3 的初始 seed 谁来写？
- **方案 A**（推荐）：Chroma 2 schema 部分由 Schema-aware Agent 自动生成；成功示范由 LLM 跑测试集 + 人工挑 30 条；错误经验冷启动为空，靠 Reflection 累积

### Q9（新）：Chroma 3 给 Planner 的 few-shot 是默认调用还是按需？
- **方案 B**（推荐）：Planner 自评置信度低于阈值时才查，或新意图首次出现时才查

**置信度的实现（补强）** —— 不让 LLM 自己说"我有多少把握"（容易作弊）。改用规则：
- 把当前 `(intent 集合, branch 组合)` 作为 key 去 Chroma 3 查 `kind=workflow_success` 的命中数
- 命中数 ≥ 3 → 高置信，不查 few-shot
- 命中数 < 3 或为 0 → 低置信，召回 top-K 示范对作为 few-shot
- 失败补救：若本次 Critic 拒绝过一次，本轮 retry 时强制查 Chroma 3

### Q10（新）：Router 是 LLM 还是规则？
- **方案 A**（推荐）：规则优先（关键词 / intent 字典） + LLM 兜底（规则未命中时）。规则部分轻量、可观测；LLM 部分给灵活性

---

## 7. 风险登记

| 风险 | 影响 | 缓解 |
|---|---|---|
| 删 24 stage 破坏现有测试 / sample_runs | 测试一片红 | Phase 1 用 v2 文件并存；测试在 Phase 4 末统一迁移 |
| Schema-aware Agent 偏差导致字段命名混乱 | 数据难查 | 用 JSONB；Schema Agent 输出走人工 review 入口 |
| NL2SQL 幻觉 / SQL 注入 | 拖垮 DB / 安全 | 强制只读 + 白名单 + LIMIT + timeout |
| Hybrid 检索成本（rerank + 双索引） | 延迟从 ~2s 升到 ~5s | rerank 仅 top-50；用本地 bge-reranker |
| 新 Planner 不确定性更高 | 失败模式难复现 | bounded 上限 + 详尽 logging + Reflection 兜底 |
| Reflection 知识库污染（错误经验越攒越偏） | 查询质量下降 | TTL 30 天 + 人工审核入口 + 反例标记 |
| 多模态成本失控 | 月度账单暴涨 | 采样策略（Q6 方案 B）+ 月度预算告警 |
| 删 capabilities/ 后 API `/capabilities/{name}` 失效 | 外部集成断裂 | 旧 endpoint 保留一段时间，内部转发到新链路 |
| Schema-aware Agent 输出和 Chroma 2 不同步 | NL2SQL 永远找不到字段 | **已采纳**：事务封装双写 + schema 指纹比对 + `tests/test_schema_consistency.py` 三项测试 + `scripts/rebuild_chroma2_schema.py` 重建命令 |
| Posts 不向量化后无法做语义检索 | "找相似帖子"类问题答得差 | Postgres tsvector + pg_trgm 兜底；监控覆盖率，必要时上 pgvector |
| Chroma 3 编排示范偏向某些组合，导致新场景失效 | Planner 出现"路径依赖" | 冷启动 seed 覆盖 ≥ 6 种 branch 组合；定期审查命中分布 |
| Router 和 Planner 职责重叠引发死循环或重复决策 | 编排不稳定 | 5c 节边界硬约束，单测覆盖每条边界 |
| Critic 错误归因偏差（NL2SQL 错被记到 Chroma 3） | 错经验灌错地方 | Critic 输出结构化 error_kind 字段；归因逻辑写程序化规则不靠 LLM |

---

## 7c. 决策审计 — 我看到的不一致 / 需要修正的地方

> 这一节是我对你刚给出的 Q1-Q10 决策做的完整 audit，列出与前文已有内容的不一致或可能的遗漏。

### A. Q7 决策 vs 5b 节"Posts 不向量化的影响和补偿"（已修正）
原文 5b 节说"通过 NL2SQL → Postgres tsvector + pg_trgm 做关键词匹配（覆盖 80% 场景）"。你的 Q7 决策是"按 topic_id 找同 topic 帖子 + Kuzu entity 关联"，逻辑路径不同。
→ **已修正**：5b 节改为 topic_id JOIN + entity-graph 为主路径，tsvector + pg_trgm 退为兜底，并加了 sparse_topic 监控约束。

### B. Q9 "置信度"如何实现没明确（已补强）
"自评置信度低于阈值"如果让 LLM 自己说，容易作弊（LLM 倾向报告高置信度，否则它自身的输出会被怀疑）。
→ **已修正**：改为规则版本 —— 用 `(intent, branch_combo)` 在 Chroma 3 历史成功记录里查命中数，命中 ≥ 3 高置信，否则查 few-shot。

### C. 7b-③ NL2SQL 内部校验和 Critic 的边界（已对齐）
你提出 "NL2SQL 通过自校验就不会是 NL2SQL 的问题"。原文 Critic 的 error_kind 列表把 SQL 语法/列名错也放了进来，会污染 Chroma 2。
→ **已修正**：分了"层级 1: NL2SQL 内部校验"和"层级 2: Critic 校验"两层；前者错误不进 Reflection；后者只保留 6 类高层错误。

### D. 7b-⑤ 自动剔除如何确认"是这条记录导致的错误"（已补强）
你说"查明原因是这个记录导致的就删除"，但没明确**怎么查明**。LLM 自查有归因偏差风险。
→ **已修正**：用 Ablation —— 不带这条 record 重跑一次，若通过即归因；并加 24h 反震荡机制。Ablation 成本：每次 Critic 失败最多 3 次重跑。

### E. 7b-⑤ 实施时机前移（已修正）
你强调"这个机制是必须要做的"。原方案放 Phase 5（最后），但**Chroma 2 在 Phase 3 NL2SQL 上线时就开始写**，等到 Phase 5 才有清理机制，中间会污染严重。
→ **已修正**：自动剔除 + 冲突替换前移到 Phase 3，Phase 5 只做 Reflection 看板和监控。

### F. Post 去重方法（替你拍板）
7b-② 你让我决定。已选 **simhash 主 + pg_trgm 兜底**（详见 5b 节末尾）。理由 3 条：simhash 入库快 100×；topic 阶段已经算过 embedding，不重复算；项目已决定 posts 不向量化。

### G. Chroma 2 Schema 部分需要在 Phase 1 / Phase 2 就有"列描述"语料
Q4 决策"LLM 自动生成种子 + 人工审核"是针对 NL2SQL 成功示范。但 schema 部分（每列的人话描述）是 Schema-aware Agent 的自动产出 —— **它需要 sample_values 才能生成靠谱的描述**。
→ Phase 2 实施时：Schema Agent 必须从该列拉至少 5 个非 null sample 给 LLM，让 LLM 看实例后再写描述。这个细节已经在 Phase 2 加了 `(table_name, column_name, description, sample_values)` 契约里。**无需再改文档**，但实施时要严格执行。

### H. 冲突替换的 LLM 比对触发阈值（已确认）

**已采纳方案 B**：只在 metadata 同主键 + embedding 余弦相似度 > **0.95** 时才触发 LLM 成对比对。
- < 0.92：直接追加，不做冲突检测
- 0.92 – 0.95：按"7b-⑤ 冲突检测"规则视为同语义槽位，但**不**调 LLM，直接覆盖
- ≥ 0.95：触发 LLM 成对比对，LLM 判定 `is_conflicting=True` 才覆盖；`False` 则两条共存
- 这样冲突 LLM 调用频率 ≈ 5%（实测后调阈值），既能挡住明显冲突又不爆成本

**注意**：原 7b-⑤ 节里写的"cosine > 0.92"为初步阈值，实际生效以本节为准（< 0.92 不动；0.92–0.95 直接覆盖；≥ 0.95 LLM 仲裁）。这个三段式逻辑落在 `services/nl2sql_memory.py` 的 `upsert_with_conflict_check()` 方法里。

---

## 7b. 看你修改后我额外发现的 5 个需要补强的点

> 这些是基于你把 Chroma 拆成 3 个、posts 不向量化之后**新引出的问题**，原方案没覆盖。

### ① Schema-aware Agent 必须双写 ✅ 已采纳
你修改后强调了"PostgreSQL 通过 NL2SQL 增删查改"。但 NL2SQL 看不到 schema 就写不出 SQL。**Schema-aware Agent 在写 PG 的同时必须把 schema 描述同步到 Chroma 2**，否则整条链路第一次查询就崩。Phase 2 已加了这个契约，并采纳了**双写一致性测试 + 重建命令**：
- 事务封装双写（staging + atomic swap）
- Schema 指纹比对（sha256）
- 三项 pytest 一致性测试，进 CI
- `scripts/rebuild_chroma2_schema.py` 兜底重建（支持 `--dry-run` / `--keep-experience` / `--full-reset`）
- 连续 3 次召回为空触发监控告警

### ② Posts 不向量化的影响范围
原本 Chroma posts collection 服务 4 类场景：
1. evidence retrieval 召回相似帖子 —— **已无**，但 Chroma 1 只装官方源，**社区帖证据要靠 NL2SQL 全文索引**
2. claim 去重 —— **影响**：现在这件事必须移到 Postgres 里做（用 simhash 列 / `pg_trgm` 相似度）
3. topic_cluster —— **不影响**（聚类用临时 embedding，不用持久 collection）
4. 用户问"找类似那条帖子" —— **影响**：依赖 tsvector 关键词匹配 + 模糊匹配，语义近义词覆盖会变弱

建议在 Phase 2 增加 `simhash` 列 + GIN 索引，把帖子去重彻底搬到 PG。
topic聚类都用embedding模型，之后找类似的帖子默认是找相同topic的帖子，claim去重你觉得怎么做好，是用embedding还是用 simhash 列 / `pg_trgm` 相似度，你决定吧

### ③ Reflection 的双轨写入需要一个"归因器"（已根据你的反馈调整）

你指出 NL2SQL 内部已有结果校验环节，所以 SQL 语法/列名错误不应进入 Critic —— 它们在 NL2SQL 内部 repair 循环里已经处理掉了。**修订后的归因层级**：

**层级 1：NL2SQL 内部校验（不写 Reflection，写 NL2SQL 内部 retry log）**
- `sql_syntax_error` —— SQL 语法错
- `sql_unknown_column` —— 表/列名错（说明 Chroma 2 的 schema 部分过期）→ 触发 schema 重建
- `sql_type_mismatch` —— 数据类型不匹配
- `sql_timeout` —— 超时
- 这些都在 `repair_sql()` 循环里自纠（最多 3 轮）；3 轮失败才升级到 Critic

**层级 2：到达 Critic 的错误（结构化归因）**
```python
class CriticVerdict:
    passed: bool
    error_kind: Literal[
        "sql_empty_result",     # NL2SQL 通过自校验但返回空，可能是查询意图与 schema 不匹配 → 写 Chroma 2
        "missing_branch",       # Planner 漏调必要分支 → 写 Chroma 3
        "wrong_branch_combo",   # Planner 选错组合 → 写 Chroma 3
        "citation_missing",     # 报告有断言无引用 → 写 Chroma 3 (writer 问题)
        "numeric_mismatch",     # 报告数字和 SQL 结果对不上 → 写 Chroma 3 (writer 问题)
        "off_topic"             # 报告没回答原问题 → 写 Chroma 3 (planner 或 writer)
    ] | None
    failed_branch: Literal["nl2sql", "kg", "evidence", "planner", "writer"] | None
    causal_record_ids: list[str]  # 本次用到的 Chroma 2/3 经验条目 ID（供 ⑤ 自动剔除使用）
```

**NL2SQL 结果校验的具体内容**（明确化）：
- 行数合理性：返回 0 行时给 LLM 重新评估"是数据真没还是查询写错了"
- 列名匹配查询意图（LLM 评估）
- 数据类型符合预期 schema
- 总行数 ≤ LIMIT，且不是命中 LIMIT 上限（命中说明可能漏数据）

`reflection_store.py` 完全按 `error_kind` 路由，不让 LLM 决定写到哪。

### ④ Chroma 3 的"模块功能描述卡"是代码契约  同意
Planner 每次启动都要知道当前系统有哪几个 branch、每个 branch 接受什么参数、返回什么结构。这些不能写死在 prompt 里 —— 应该作为 `ModuleCard` Pydantic 由各 branch 自描述，启动时同步到 Chroma 3。建议结构：
```python
class ModuleCard:
    name: str                # "evidence_retrieval"
    description: str         # 一句话说明
    when_to_use: list[str]   # 适用场景
    input_schema: dict       # JSON schema
    output_schema: dict      # JSON schema
    examples: list[dict]     # 输入输出示例
```
Phase 3 每个 branch 实现时同步交付 ModuleCard。

### ⑤ Chroma 经验库需要"评分 + 衰减"机制（已根据你的反馈调整为自动剔除 + 冲突替换）

按你的方向修订为**全自动闭环**，无人工干预：

**经验条目的 metadata**：
- `confidence: float`（初始 0.5；每次命中且最终 Critic 通过 +0.1；被归因为致错 → 直接删除）
- `last_used_at: ts`（30 天没命中自动下架）
- `hit_count: int`（监控用）
- `record_id: str`（UUID，供 CriticVerdict.causal_record_ids 引用）

**自动剔除流程**（致错记录清除）：
1. 每次 NL2SQL / Planner 调用时，把召回的经验条目 `record_id` 一起传下去，挂在 `CriticVerdict.causal_record_ids`
2. Critic 拒绝 + 错误归因明确后，`reflection_store.py` 走**Ablation 验证**：
   - 不带这条 record 重新跑一次 NL2SQL / Planner
   - 若结果通过 Critic → 这条 record 是元凶 → 直接删除，并把本次纠错经验作为新条目写入
   - 若仍失败 → 不归因到这条 record，转写为新错误经验
3. Ablation 成本控制：每次 Critic 失败最多对 top-3 召回的 record 做 ablation，避免成本爆炸

**冲突检测与替换**（新经验写入时）：
1. 准备写入新经验前，先在 Chroma 2 / 3 召回相似度最高的 top-5 现有条目
2. **冲突判定规则**（必须同时满足）：
   - Metadata 同 `kind` + 同主键（NL2SQL: 同 `table_name`；Planner: 同 `branches_used`）
   - Embedding 余弦相似度 > **0.92**
   - 内容上结论相反（如：旧条目说"用 LEFT JOIN"，新条目说"用 INNER JOIN"，对同一查询）—— 用 LLM 做一次成对比对，输出 `is_conflicting: bool`
3. 命中冲突 → 删除旧条目，写入新条目
4. 未命中 → 直接追加

**防震荡**（必加）：
- 同一 record_id 在 24h 内被反复"删除→重写→删除"超过 2 次 → 标记 `quarantined`，停止再写入这个语义槽位 24h
- 维护一个 24h 滚动的 `record_history` 表，监控震荡

**实施位置**：本来建议放 Phase 5，按你的意见**前移到 Phase 3**（NL2SQL 上线时就具备自剔除能力，否则 Chroma 2 会很快被脏数据污染）。

---

## 8. 我的建议（综合考虑）

下面是我对方案各处的倾向，仅供你定稿时参考：

1. **总体节奏**：5 个阶段太理想化，实际建议先做 **Phase 1 + Phase 2 的最小垂直切片**（只跑 fixture → 多模态 → topic 聚类 → 三库写入），跑通后再扩展。第一个迭代不超过 2 周。

2. **保守原则**：
   - **Schema 用 JSONB**（Q2-A），不要真动态 ALTER
   - **官方源先白名单**（Q3-A），不要自建爬虫
   - **多模态只对高互动帖子做**（Q6-B），别一开始就全开
   - **干预/反制先 deprecated 不删**（Q1-B），万一新链路不达预期还能回退

3. **激进原则**：
   - **Capability 抽象层删掉**（Q5-A），保留只会增加迁移复杂度
   - **Claim 5 档裁决删掉**，让 RAG 综合判断更自然，避免重复维护两套裁决逻辑

4. **关键投资点**（值得多花时间打磨）：
   - **NL2SQL 经验 RAG**：这是图中最有特色也最容易出问题的部分。建议种子语料人工写 100 条，并设立"周更"机制
   - **Hybrid 检索的 Rerank**：影响所有 Evidence 类回答的质量。本地化 bge-reranker 比 API 更可控
   - **质量校验 Agent**：是减少幻觉的关键。Citation 一致性检查应该写成程序化规则（不全靠 LLM）

5. **测试策略**：
   - 现有 37 个 chat 测试在 Phase 4 末统一重写，不要边改边修
   - 每个分支（RAG/SQL/KG）至少要有 5 个端到端测试 + 5 个对抗测试（故意给奇怪的查询）
   - Reflection 需要单独的回归测试（错误经验是否真的避免了同类错误）

6. **可观测性**：
   - 每次 Chat 调用记录：rewrite 输出 / planner 选择的分支 / 每个分支耗时 / critic 是否拒绝 / final answer
   - 加一个 `/admin/traces` 看板（可在 Phase 4 末追加）

7. **跳过的部分**：
   - 图中没画出"用户身份认证 / 多租户 / 限流"，假设暂不做
   - "用户已有历史信息更新"那个分支（图中底部）含义不太明确，建议归到 Reflection 范畴

8. **文档同步**：方案定稿后，需要同时更新：
   - `PROJECT_OVERVIEW.md`
   - `README.md`（快速开始那部分）
   - `docs/architecture.md`
   - 删除 `complete_project_transformation_plan.md`（被本文档替代）

---

## 9. 待确认事项 Checklist

请你审阅后在下面打勾或修改：

- [ ] 整体方向（图中流程）认可
- [x] Q1 决策：方案 A — 完全删除
- [x] Q2 决策：方案 A — JSONB
- [x] Q3 决策：方案 A — 5 站白名单
- [x] Q4 决策：方案 C — LLM 自动生成种子，人工审核
- [x] Q5 决策：方案 A — 完全删除 capabilities/
- [x] Q6 决策：方案 B — 只对高互动帖子做
- [x] Q7 决策（新）：按 topic_id 找同 topic 帖子 + Kuzu entity 关联（已落 5b 节）
- [x] Q8 决策（新）：方案 A — schema 自动 + 30 条 LLM 跑 + 人工挑 + error 留空
- [x] Q9 决策（新）：方案 B — 按需调用，置信度用规则（命中数 ≥ 3）实现
- [x] Q10 决策（新）：方案 A — 规则优先 + LLM 兜底
- [x] Q11 决策（新）：方案 B — metadata 同主键 + embedding 余弦 > 0.95 才触发 LLM 比对（三段式：< 0.92 追加 / 0.92–0.95 直接覆盖 / ≥ 0.95 LLM 仲裁）
- [ ] 阶段拆分认可（5 阶段 / 4-6 周）
- [ ] 是否先做 MVP 垂直切片（Phase 1+2 最小版）
- [ ] 是否需要新建 `branches/redesign-2026-05` 分支隔离改造
- [ ] 7b 节的 5 个补强点是否要并入主方案
- [ ] 还要补充 / 修改的地方：

定稿后请直接在本文档基础上修改，或贴一份你的最终修改文档给我，我据此开 Phase 1。

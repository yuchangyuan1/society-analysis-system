
、# 知识图谱模块改造方案

> **状态**：待评审
> **日期**：2026-05-02
> **背景**：当前 KG 模块只重画了 PG 的 `(account_id, post_id, topic_id)` 列，
> 0 条 `Replied / Liked` 边，4 类 KG 查询里 3 类能被 SQL 平替，1 类（传播路径）
> 因为没 reply 数据无法工作。把它升级成项目真正的"传播分析"护城河。

---

## 0. 改造目标

> **从"等价于 SQL 视图的图重绘"** 变成 **"传播分析专属的不可替代基础设施"**

衡量标准：
1. **数据真度**：每次 v2 pipeline run 后 Kuzu 中至少有一条 `Replied` 边（前提是源数据有回复）
2. **算法独占**：≥ 3 个 KG 查询是 SQL **写不出来或显著低效** 的
3. **接入主链路**：Planner 在某些 intent 下**优先**调 KG 而不是 NL2SQL（不是"加上 KG"）
4. **延迟**：单次 KG 查询 P95 ≤ 800ms（典型 1-2K post 规模）

---

## 1. 当前状况盘点

### 1.1 数据层
- Kuzu schema 已定义节点 `Account / Post / Topic / Entity` 和关系 `Posted / Replied / Liked / BelongsToTopic / HasEntity`
- 实际写入：仅 `Posted` + `BelongsToTopic` + `HasEntity`
- **Replied / Liked 0 条** —— `agents/ingestion.py` 没拉 reply 链；`Liked` 没数据源

### 1.2 查询层
`tools/kg_query_tools.py::KGQueryTool` 4 类查询：
- ✅ `key_nodes(topic, top_k)` —— 工作但与 NL2SQL `GROUP BY` 等价
- ✅ `community_relations(topic, min_shared)` —— 工作但与 NL2SQL self-join 等价
- ✅ `topic_correlation(a, b)` —— 工作但与 NL2SQL 三表 JOIN 等价
- ❌ `propagation_path(a, b, max_hops)` —— 唯一不可替代但**当前永远返回空**

### 1.3 编排层
`BoundedPlannerV2._BranchRouter`：
- `propagation → [kg, nl2sql]`
- `comparison → [evidence, nl2sql, kg]`
- 其它 intent 不带 kg → KG 经常不被调用

### 1.4 模块描述层
Chroma 3 `kind=module_card` 里 KG 卡片描述只列了 4 类查询，没说"什么场景必须用 KG"

---

## 2. 改造路线（4 阶段）

### Phase A — 数据真度（≈ 1-2 天）

**目标**：给 KG 补 `Replied / Liked` 边数据。

#### A.1 Reddit reply 抓取
当前 `services/reddit_service.py` 拉 listing 时丢弃了 `parent_id`。改：

| 文件 | 改动 |
|---|---|
| `services/reddit_service.py` | 拉 post comment 树（Reddit JSON `comments[]`），把每条 comment 当作子 Post |
| `models/post.py` | 加 `parent_post_id: Optional[str] = None` 字段 |
| `agents/ingestion.py::_store_post` | 当 `post.parent_post_id` 非空时，调 `kuzu.add_replied(post.id, post.parent_post_id)` |
| `agents/precompute_pipeline_v2.py::_persist_v2` | 同样调 `add_replied` |

**Reddit 树深度限制**：默认抓 top 100 comments / post，max_depth=3，过深的 comment 当孤儿不接 reply 边

#### A.2 Telegram reply 抓取
`telethon` 的 `Message.reply_to_msg_id` 已经在原始 message 里，直接映射

#### A.3 Liked 边（可选；当前数据源没有）
跳过 —— Reddit JSON 不暴露具体哪个用户点了赞。`Post.like_count` 已经是聚合数。
**结论**：删除 `Liked` 关系定义，不维护没数据源的虚假表

#### A.4 fixture 覆盖
新增 `tests/fixtures/posts_v2_reply_chain.jsonl`：5 条 post，构成 a→b→c 链 + b→d 分叉
让 `propagation_path` 第一次能真正出结果

#### A.5 验收
```bash
python main.py --jsonl tests/fixtures/posts_v2_reply_chain.jsonl
python -c "
import config, kuzu
db = kuzu.Database(config.KUZU_DB_DIR); con = kuzu.Connection(db)
print('Replied:', con.execute('MATCH ()-[:Replied]->() RETURN count(*)').get_next())"
```
应该看到 ≥ 4 条 Replied 边。

---

### Phase B — 算法独占（≈ 3-5 天）

**目标**：加 SQL 做不到的图算法查询，让 KG 真正"必须存在"。

Kuzu 0.7 还没有原生 PageRank / Louvain，但项目既然要做传播分析，**重新引入 NetworkX 作为内存图算法库**是合理的（不引入图存储模型，仅用算法）。

> **重要决策**：上 Phase 5 我们删了 networkx 依赖。这里要重装，并明确分工：
> - **Kuzu**：持久化 + Cypher 关系遍历（多跳 / 子图匹配）
> - **NetworkX**：从 Kuzu 拉子图后跑 PageRank / centrality / community detection
> - 不再让 NetworkX 持久化任何状态，只做 in-memory 计算

#### B.1 新增查询类型

| `query_kind` | 用途 | 算法 | SQL 能不能做 |
|---|---|---|---|
| `propagation_path` | 两账户间最短 reply 路径 | Kuzu Cypher k-hop | ❌ 多跳 JOIN 写不出来 |
| `cascade_tree` | 一条 post 引发的整棵 reply 树 | Kuzu 递归 traversal | ❌ 同上 |
| `influencer_rank` | topic 内账户的 PageRank | NetworkX `pagerank` | ❌ 迭代算法 |
| `bridge_accounts` | 跨社区桥接节点 | NetworkX `betweenness_centrality` | ❌ 同上 |
| `coordinated_groups` | 同步发帖的账户簇 | NetworkX `community.louvain_communities` | ❌ 模块度优化 |
| `viral_cascade` | 传播速度 + 影响范围 top-N | Kuzu cascade tree + 时间窗口分析 | ❌ 路径上的时间统计 |
| `echo_chamber` | 回复主要落在内部的封闭群组 | NetworkX modularity ≥ 阈值 + entity 重合度 | ❌ 同上 |

保留 / 重构：
- `key_nodes` ：保留但语义升级到"按 PageRank 排序"而不是 post_count
- `community_relations` 删除（被 `coordinated_groups` 取代）
- `topic_correlation` 保留（图遍历能做，SQL 也能做，但放 KG 一致性更好）

#### B.2 新增模块结构

```
tools/kg_query_tools.py
├── KGQueryTool                  # 简单 Cypher 查询（已有，重构）
└── 新增方法：
    ├── propagation_path(a, b, max_hops=6)
    ├── cascade_tree(post_id, max_depth=10)
    └── viral_cascade(topic_id, top_k=10)

agents/kg_analytics.py            # 新文件
└── KGAnalytics
    ├── influencer_rank(topic_id, top_k=10)
    ├── bridge_accounts(top_k=10)
    ├── coordinated_groups(min_size=3)
    └── echo_chamber(modularity_threshold=0.4)
```

`agents/kg_analytics.py` 职责：
1. 从 Kuzu 拉 topic / 全局子图（`MATCH (a)-[r:Posted|Replied]->(b) RETURN ...`）
2. 用 NetworkX 构图（DiGraph）
3. 跑算法
4. 把结果转回 `KGOutput`（节点 + 边 + metrics）

#### B.3 子图缓存
重复跑 PageRank 太贵，加缓存：
- `services/kg_cache.py`：LRU(8)，key 是 `(topic_id, last_kuzu_write_ts)`
- 大子图取一次，多个算法共用

#### B.4 依赖
重新加回 networkx：
```bash
pip install "networkx>=3.0" "python-louvain>=0.16"
# pyproject.toml 也加回这两个
```

#### B.5 测试
`tests/test_kg_analytics_v2.py`：
- 用 fixture 构 reply 树（10 帖，2 个明显的 community）
- 验证 `influencer_rank` 第一名是中心节点
- 验证 `coordinated_groups` 分出 2 个 community
- 验证 `bridge_accounts` 找到桥节点
- 算法不可达（NetworkX 缺包）时降级到 Cypher-only

---

### Phase C — 编排层接入（≈ 1 天）

**目标**：让 Planner 真正会用 KG，而不是把它当 nl2sql 的备胎。

#### C.1 扩展 intent 集
`agents/query_rewriter.py` 加 5 个新 intent：

| 新 intent | 默认分支 | 用户提问例 |
|---|---|---|
| `propagation_trace` | `[kg]` | "show me how this rumor spread" / "trace the reply chain" |
| `influencer_query` | `[kg, nl2sql]` | "who's most influential" / "top spreaders" |
| `coordination_check` | `[kg]` | "is this organized" / "coordinated posting" |
| `community_structure` | `[kg, nl2sql]` | "echo chamber" / "are these accounts in the same group" |
| `cascade_query` | `[kg]` | "viral", "longest thread", "deepest reply chain" |

`models/query.py::SubtaskIntent` 同步加这些值

#### C.2 Rewriter prompt 更新
显式列出新 intent 关键词触发条件，并强调"propagation / amplifier / cascade / echo chamber 等问题应优先 KG"

#### C.3 ModuleCard 升级
`services/planner_memory.py::SEED_MODULE_CARDS` 里 KG 卡更新：
- `description`：强调"the only branch that does multi-hop / centrality / community detection"
- `when_to_use`：列出 5 个新 intent + 关键短语
- `when_not_to_use`：明确 "simple counts / filters → use nl2sql instead"
- 加 5 条新 examples

#### C.4 Workflow exemplars
`SEED_WORKFLOW_EXEMPLARS` 加 5 条 KG-heavy 示范，让 Chroma 3 召回更倾向多分支组合

#### C.5 重新 seed
```bash
python -m scripts.seed_planner_memory
```

---

### Phase D — 可视化 + 观测（≈ 1-2 天）

**目标**：用户在 UI 真的能"看见"图，不只是看 JSON。

#### D.1 Streamlit Graph Tab 升级
`ui/components/analysis_tabs.py` 的 Graph Tab 当前只 dump JSON。改：

- 用 `pyvis` 或 `streamlit-agraph` 把 `KGOutput.nodes / edges` 渲染成可拖拽节点图
- 节点颜色按 community / role
- 边粗细按权重（reply count）
- 点击节点显示该 account 的最近 5 条 post

(这一步需要重新装 pyvis；上次精简时删了，可以确定地为 KG 一定用得上而装回)

#### D.2 KG 健康检查 endpoint
`api/routes/health.py` 加 `/health/kg`：
- node 数 / edge 数 per 类型
- 最近一次 reply 边写入时间
- 4 个查询类型的 P95 延迟（最近 100 次）

#### D.3 Reflection 看板加 KG tab
当 KG 查询返回空 / 异常率高时显式标红，方便排查"是数据没填够还是 query 写错了"

---

## 3. 总成本估算

| 阶段 | 工时 | 新增 LLM 调用 | 新依赖 |
|---|---|---|---|
| A 数据真度 | 1-2 天 | 0 | 无 |
| B 算法独占 | 3-5 天 | 0（NetworkX 不调 LLM） | networkx, python-louvain |
| C 编排接入 | 1 天 | 0（仅修改 prompt + seed） | 无 |
| D 可视化 + 观测 | 1-2 天 | 0 | pyvis（或 streamlit-agraph） |
| **合计** | **6-10 天** | 0 | 3 包 |

不引入 LLM 调用是这个改造的关键优势 —— **KG 部分完全确定性**，不像 NL2SQL / Critic 那样需要 prompt tuning。

---

## 4. 改造后的能力对比

| 用户提问 | 改造前 | 改造后 |
|---|---|---|
| "Who's most active in topic X?" | KG group_by post_count 等价 SQL | KG PageRank（按影响力，不是发帖量） |
| "Is this rumor coordinated?" | 无能力 | `coordinated_groups` Louvain 输出簇 |
| "Trace the reply chain" | 永远空 | `propagation_path` 真实路径 |
| "Find bridge accounts" | 无能力 | `bridge_accounts` betweenness centrality |
| "Show the viral cascade" | 无能力 | `cascade_tree` + 时间窗口分析 |
| "Are these two topics linked?" | 三表 JOIN | 共享 entity + Cypher 路径长度 |

---

## 5. 拍板项

请确认以下决策方向，再开 Phase A：

1. **是否删除 `Liked` 关系**（无数据源，维护成本无收益）—— 建议 ✅ 删除
2. **NetworkX 重装**（删 → 加回，因为 KG 算法需要）—— 建议 ✅ 装回
3. **`community_relations` 删除还是保留**（被 `coordinated_groups` 完全覆盖）—— 建议 ✅ 删除，避免两个语义类似的 query_kind 让 Planner 困惑
4. **Phase D 可视化用 pyvis 还是 streamlit-agraph**（pyvis 输出 HTML，streamlit-agraph 是原生组件）—— 建议 streamlit-agraph，Streamlit 集成更顺
5. **Reddit comment 抓取深度**（当前不抓 reply）—— 建议 max 100 comments × max_depth 3，平衡成本和数据完整度
6. **是否需要新建 `redesign-2026-05-kg` 分支隔离改造** —— 建议 ✅ 新建分支，4 阶段累计改动较大

---

## 6. 验收标准（Definition of Done）

Phase A：
- [ ] Reddit reply 链能拉到 Kuzu，新跑一次 pipeline 后 `MATCH ()-[:Replied]->() RETURN count(*)` ≥ 1
- [ ] Telegram reply 链能拉到 Kuzu
- [ ] 删除 `Liked` 关系定义和所有引用
- [ ] `tests/fixtures/posts_v2_reply_chain.jsonl` + 单测通过

Phase B：
- [ ] `agents/kg_analytics.py` 4 个新算法实现 + 单测通过
- [ ] `tools/kg_query_tools.py` `propagation_path / cascade_tree / viral_cascade` 工作
- [ ] `services/kg_cache.py` LRU 命中率监控可见
- [ ] 单次大子图算法 P95 ≤ 800ms（10K node / 50K edge 规模）

Phase C：
- [ ] 5 个新 intent 在 Rewriter 中触发正确
- [ ] ModuleCard / WorkflowExemplar 重新 seed
- [ ] Planner 在 propagation 类问题上调 KG 而不是 NL2SQL（用 `branches_used` 验证）

Phase D：
- [ ] Streamlit Graph Tab 显示真实图（可拖拽）
- [ ] `/health/kg` endpoint 返回完整诊断信息
- [ ] Reflection 看板能看到 KG 查询失败率

---

## 7. 与现有文档同步

完成后需更新：
- `workflow.md` —— Tools 表加新 `KGAnalytics`，配置表加缓存相关 env
- `README.md` —— 新增"传播分析"使用示例
- `PROJECT_REDESIGN_V2.md` —— 在尾巴加一节 "Phase 7: KG redesign"
- `pyproject.toml` —— 加回 networkx / python-louvain（+ 可视化包）
- `.env.example` —— 加 `KG_CACHE_SIZE` 等

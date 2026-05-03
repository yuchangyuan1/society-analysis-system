# 生产化前的修改方案

> **状态**：等待评审 / 拍板
> **日期**：2026-05-03
> **背景**：完成 Phase A→D 知识图谱改造后，从生产视角盘点出 8 类问题。
> 本文档把每个问题的影响、修法、工时、决策点列清楚，等你逐项拍板后再动手。
>
> **回复方式**：每个 Decision 块后面加一行 `[决策]: ...` 即可，我据此实施。

---

## 优先级总览

| Priority | 问题 | 修法工时 | 阻塞 |
|---|---|---|---|
| **P0** | #1 数据所有权混乱（无 run_id 贯穿） | 2 天 | "昨天回答今天不一样" |
| **P0** | #2 Kuzu 单进程嵌入式（无法多 worker） | 1.5 天 | 一上多 worker 就死锁 |
| **P0** | #3 缺删除 / 修复路径 | 0.5 天 | 没法运维，GDPR 不能合规 |
| **P1** | #7 安全 / 多租户（API 裸奔 / NL2SQL 不强制只读） | 0.5 天 | 公网部署当天出事 |
| **P1** | #6 LLM 失败级联（无统一 retry / circuit breaker） | 1 天 | OpenAI 限流抖动 |
| **P1** | #4 没有 freshness 概念 | 0.5 天 | 趋势分析失真 |
| **P2** | #5 监控盲区 | 1 天 | 出问题查不出 |
| **P2** | #8 Pipeline 事务 / 回滚 | 0.5 天 | 中途崩三库漂移 |

**MVP 总工时**：约 7.5 天（P0+P1）。P2 可以边跑边补。

---

## #1 数据所有权混乱（P0）

### 现状
- 每次 `python main.py --jsonl X` 都新建 `topic_id` (UUID 随机)、新 `chunk_id`
- PG `posts_v2` 用 `ON CONFLICT DO UPDATE` → 同一 post 第二次跑被覆盖
- Kuzu 用 `MERGE` → 同一 post 节点保留旧值，但属性可能被新文本覆盖
- Chroma 用 `upsert` → 同一 chunk_id 替换
- 三库**没有 `run_id` 贯穿**，无法回答"这条数据是哪一次跑得到的"

### 真问题（生产场景）
1. **同一个 Reddit 帖子第二天再抓**：topic_id 可能变了 → "昨天 vaccine topic 的活跃账户"得不到稳定答案
2. **schema 漂移**：第二次 pipeline 把 extra JSONB 字段重命名了，旧帖的 extra 字段还在但 NL2SQL 不知道
3. **可重复性**：用户报告"3 月 5 日的回答错了"，没法定位是哪个 run / 哪个 schema 版本

### 修法
1. 给 `posts_v2 / topics_v2 / entities_v2` 加 `first_seen_in_run / last_updated_in_run` 列（已有 manifest 但没贯穿）
2. **`topic_id` 用内容指纹**（normalised text 的 sha256[:12]）而不是随机 UUID
   - 跨 run 稳定：同一组帖子聚类后 topic_id 一致
   - 缺点：聚类质量微变就会产生新 topic_id
3. Kuzu Post 节点加 `last_run_id` 属性
4. Chroma metadata 加 `source_run_id`
5. `RunManifest.commit_state: pending | committed | failed` 配 atomic flag

### 拍板项
- **D1.1 topic_id 用内容指纹（cluster member post id 排序后 hash）还是保留 UUID？**
  - 内容指纹：跨 run 稳定，但 cluster 边界一变 topic 就变；适合"trending 持续追踪"
  - UUID：每次 run 完全独立，适合"快照式"分析
  - 我推荐：**内容指纹**（生产场景需要稳定 topic）

[决策]: 用内容指纹

- **D1.2 Reddit 帖子已经有原生稳定 id，不用改。但 fixture jsonl 的 id 由用户提供，要不要校验唯一性？**

[决策]: 不需要加唯一性校验

- **D1.3 旧数据迁移：现有 PG/Kuzu/Chroma 已有数据，加 `first_seen_in_run` 列时填什么？**
  - 选项 A：填 `"legacy_pre_v6"`，所有现存数据归为一个虚拟 run
  - 选项 B：清空 PG/Kuzu/Chroma 重跑（最干净）
  - 我推荐：**选项 A**，避免破坏正在使用的环境

[决策]: A

---

## #2 Kuzu 单进程嵌入式（P0）

### 现状
- `KuzuService()` 默认拿独占文件锁
- `uvicorn --workers 4` 第二个 worker 直接启不来
- Streamlit / API / scheduler 各自起一个 KuzuService → 互相阻塞

### 真问题
1. 想用 4 个 worker 提高 chat 吞吐 → 做不到
2. pipeline 跑时 chat 的 KG 查询 hang 死
3. 备份 / 监控工具打开 Kuzu 文件就拒之门外其它进程

### 修法（三选一）

**A. 单 writer 串行（最轻）**
- pipeline / scheduler 持有唯一 writer，API / UI 只读模式打开 Kuzu
- 改 `KuzuService.__init__(read_only=False)`，默认 `read_only=True`
- 写时获取文件锁，读时不需要
- 工时：0.5 天
- 风险：UI / API 仍可能在 pipeline 写入时拿不到读锁（Kuzu 0.7 的 read_only 是否真的避锁，需实测）

**B. 镜像到 PG（中等，推荐）**
- KG 的核心数据（reply 边 / account-account 邻接）双写到 PG `reply_edges` 表
- KG 算法（PageRank / Louvain）从 PG 拉子图，NetworkX 跑算法
- Kuzu 仍存在但只做"画图查询"和 cypher 调试用
- 工时：2 天
- 优点：彻底解锁多 worker、并发；KG 分析路径完全无锁
- 缺点：双写一致性问题（同 schema_meta 双写一致性方案）

**C. 迁到 Neo4j / Memgraph（重度）**
- client-server 图库，原生支持并发
- 工时：5+ 天
- 引入新基础设施依赖

### 拍板项
- **D2.1 选哪个方案？**
  
[决策]: A 
---

## #4 没有 freshness 概念（P1）

### 现状
- `posts_v2.posted_at` 有数据但**没人用**
- 用户问 "what's trending today" → SQL 不带时间过滤 → mix 全部历史
- KG PageRank 永远偏向老账户（活得久的累积分高）

### 真问题
- 趋势分析失真
- 用户对"今天/本周"的期待和系统行为不一致
- 长跑 90 天后 KG 的"活跃账户"全是老用户

### 修法
1. **NL2SQL prompt** 加默认 `posted_at >= NOW() - INTERVAL '7 days'` 启发式（除非 user 明确"all-time"）
2. **Rewriter** intent 加 `timeframe` 字段（已存在但未填）
3. **KG 子图** 切片：`influencer_rank(topic_id, since_days=7)` —— `agents/kg_analytics._account_reply_graph` Cypher 加 `WHERE posted_at >= $cutoff`
4. **Chroma 1** 加 `published_date` metadata filter（已经写入但 Hybrid Retrieval 没默认用）

### 拍板项
- **D4.1 默认时间窗多少天？**
  - 7 天（趋势类问题最常见）
  - 30 天（fact-check 类需要更长上下文）
  - 我推荐：**默认 7 天，trending/propagation 用 7 天，fact-check 用 30 天**

[决策]: trending/propagation 默认30天， fact check默认不过滤时间，除非用户要求

- **D4.2 历史回溯怎么触发？**
  - 选项 A：用户说 "all time" / "since beginning"，rewriter 把 timeframe 设为 null
  - 选项 B：永远不限制，让用户主动加"in the last week"
  - 推荐：**A**

[决策]: A

---

## #5 监控盲区（P2）

### 现状
- Critic 失败率？不知道
- NL2SQL repair 平均轮次？不知道
- Chroma cache 命中率？只能从 `/health/kg` 看 KG 那部分
- 每次 chat 调了几次 LLM、花了多少 token？不知道

### 修法
1. `services/metrics.py` —— Counter / Histogram 简易实现（不引 prometheus）
2. 各 agent / branch 在关键路径打点：
   - `metrics.observe("rewriter.latency_ms", ms)`
   - `metrics.inc("critic.verdict", labels={"passed": True})`
   - `metrics.inc("nl2sql.repair_rounds", labels={"rounds": n})`
3. `/health/metrics` endpoint 返回 JSON 快照
4. Reflection 看板加 "Performance" tab

### 拍板项
- **D5.1 用自建简单 Counter/Histogram，还是引入 `prometheus_client`？**
  - 自建：零依赖，输出 JSON
  - prometheus：标准化但要部署 Prometheus + Grafana
  - 我推荐：**自建**（项目目前没有 ops 基础设施）

[决策]: 自建

---


## #8 Pipeline 事务 / 回滚（P2）

### 现状
- pipeline 中途崩 → PG 部分写 + Kuzu 部分写 + Chroma 部分写 → 三库不一致
- 启动后 schema 一致性测试一定失败

### 修法
1. `RunManifest.commit_state: pending | committed | failed`
2. 每个 stage 完成后 atomic flag
3. 启动时 sweep `pending` 的 run：
   - 选项 A：**前滚**（重跑 pending stage 之后的）
   - 选项 B：**回滚**（删掉 pending run 的所有产出）
4. CLI: `python -m scripts.data_admin scan-pending`

### 拍板项
- **D8.1 前滚还是回滚？**
  - 推荐：**回滚**（数据完整性优先于"不浪费"）；前滚太复杂

[决策]: 回滚

---

## 我的请求

请逐项填 `[决策]:` 行。我**不做"我推荐"的部分**直到你确认 —— 哪怕只填"采纳所有推荐"也行（一句话）。

特别需要你拍板的（不能由我代决）：
1. **D1.1 topic_id 用内容指纹还是 UUID** —— 影响所有未来的 trend 类问题
2. **D2.1 Kuzu 方案三选一** —— 决定后续 KG 工程量
3. **D7.1 / D7.2 鉴权方案** —— 决定是否能公网部署

其它都接受我的推荐也合理。

# 另外想加的修改：
## 1. BM25 索引常驻（避免每次 retrieval 重建）
## 2. KG 增量写入

# Society Analysis System — Fixes & Solutions 审查报告

**审查范围：** 原 `fixes_and_solutions(1).md` 中提出的 7 项 patch
**审查目标：** 核对每项问题在当前代码中是否真实存在；提议方案是否正确；是否存在更完善方案；是否有应被识别为「有意设计」而不应修复的项

---

## 总体结论概览

| # | 问题 | 是否需修复 | 评估 |
|---|---|---|---|
| 1 | KG 分支不被调用（PlanVerifier R-OVERVIEW） | **不应修复** | **R-OVERVIEW 是有意设计**，已有显式测试保护此不变量；提议的"修复"会破坏现有测试 |
| 2 | KG 返回 0 节点（TopicResolver fallback） | 部分修复 | 真实问题，但提议的兜底方案在用户已明确指定具体 topic 时会返回**误导性数据**，需更精细的触发条件 |
| 3 | NL2SQL `dominant_emotion` GROUP BY 报错 | **应修复** | 真实 bug，提议的 `MAX(NULLIF(...))` 正确 |
| 4 | Official Media Recap 模式不生效 | **应修复**（方案需调整） | 真实问题，但根因是 `_question_is_fact_check` 的关键词捕获过宽；提议方案描述与实际代码结构不符（`if`/`if`，非 `elif`），且与 fact-check 关键词重叠会双触发 |
| 5 | echo_chamber UI 空图（"已知行为"） | 可一并修复 | 文档判定无需修复，但与 Issue 6 合并修复成本极低 |
| 6 | KG metrics 数字混淆（node_count 314 vs 返回 0） | **应修复**（有更优替代） | 真实问题，最佳修复是同时解决 Issue 5：让 echo_chamber 实际填充 `out.nodes`/`out.edges`，而不仅是重命名 metrics 键 |
| 7 | 数据目录迁移 / Streamlit 启动命令 | 不适用 | macOS shell 命令，本机为 Windows；非代码 patch |

---


## Issue 3 — NL2SQL `t.dominant_emotion` GROUP BY 报错【应修复】

### 代码现状（确认 bug 存在）

- `tools/nl2sql_tools.py:122-126`：系统提示模板
- `tools/nl2sql_tools.py:222-224`：`_BUILTIN_GUIDANCE` 中 `filtered_topic_dominant_emotion_from_posts` 规则
- `tools/nl2sql_tools.py:251-253`：`today_worldnews_window` few-shot 示例

三处均为裸 `NULLIF(t.dominant_emotion, '')`，未被聚合。

### 何时触发报错

- **单一聚合查询（无 GROUP BY）**：例如 `"What is the dominant emotion in Iran-US tension discussions?"` → LLM 通常生成 `SELECT COALESCE(mode()..., NULLIF(t.dominant_emotion,''), 'Not specified') FROM posts_v2 p JOIN topics_v2 t WHERE label ILIKE '%Iran-US%' LIMIT 1`，无 GROUP BY，`mode()` 是聚合，`NULLIF(t...)` 不是 → PG 报错
- **GROUP BY 仅含 `t.label`** 时：`t.dominant_emotion` 不在 PK 链路上，PG 不识别函数依赖

### 对原文档提议修复的评估

- `MAX(NULLIF(t.dominant_emotion, ''))` 把第二项变成聚合，两种情况都通过
- **边界**：当 JOIN 跨多个 topic 行时，`MAX` 返回字典序最大的情绪（如 "fear" > "anger"），而非"最常见"。但此分支仅在 post 级 emotion 全为 NULL/空时才参与，可接受
- ⚠️ Few-shot 示例（line 246-260）GROUP BY 已含 `t.topic_id, t.label`，理论上 PG 应识别 `topic_id` 是 `topics_v2` 主键时的函数依赖。但 PG 的函数依赖识别要求严格 PK 元数据，对模板变体不可靠。`MAX` 包装更鲁棒

### 更完善的方案

- 文档方案是最简洁可靠的修复，**直接采用**
- 替代 1：去掉 `t.dominant_emotion` 这一回退分支，仅保留 `mode()` 与 `'Not specified'`——会丢失"post 全部缺标注但 topic 级有标注"的真实场景，不推荐
- 替代 2：使用子查询 `(SELECT t2.dominant_emotion FROM topics_v2 t2 WHERE t2.topic_id IN (...) LIMIT 1)`——更准确但 LLM 难稳定生成

### 结论

**应修复，按文档方案。** 三处均改为 `MAX(NULLIF(t.dominant_emotion, ''))`。

### 验证命令

```bash
docker exec society_postgres psql -U society -d society_db -c \
  "SELECT COALESCE(mode() WITHIN GROUP (ORDER BY NULLIF(p.dominant_emotion,'')),
          MAX(NULLIF(t.dominant_emotion,'')), 'Not specified') AS emotion
   FROM posts_v2 p JOIN topics_v2 t ON p.topic_id = t.topic_id
   WHERE t.label ILIKE '%Iran-US%';"
# 预期: fear（不再报 GROUP BY 错误）
```

---

## Issue 4 — Official Media Recap 不切换格式【应修复，但方案需调整】

### 代码现状

- `agents/report_writer.py:633-643` `_question_is_fact_check`：
  ```python
  return any(k in text for k in (
      "fact-check", "fact check", "verify", "verification",
      "official source", "official sources", "claim", ...
  ))
  ```
  → 包含 `"official source"` 关键词 → "What does official media say about X?" 会被误归类为 fact-check
- `_build_payload`（`report_writer.py:333-346`）只对 `fact_check` 与 `claim_audit` 提示模式；无 comparison/recap 分支

### 问题真实性

真实存在。fact-check 模式输出 "Supported / Contradicted / Not found / Insufficient evidence" 四象限判定，与"概述官方报道"语义错位。

### 对原文档提议修复的评估

- 提议「在 `_build_payload` 的 elif chain 中加入 comparison 模式」——**实际代码使用的是独立 `if`/`if`（line 333、340），不是 elif**。文档描述与实际结构不符
- 提议的 `_question_is_comparison` 函数与 `_question_is_fact_check` **关键词重叠**：`"official sources say"` / `"official media"` 既出现在 fact-check 检测又出现在 recap 检测。两个 `if` 都会触发，LLM 拿到两套互斥指令
- 文档没有处理这一互斥

### 更完善的方案

1. **修改 `_question_is_fact_check` 优先级**：当 subtask intent 为 `official_recap` 或 `comparison` 时，**直接返回 False**——QueryRewriter 已经识别这两个 intent，应作为权威信号优先于关键词匹配
2. 在 `_build_payload` 加入 `comparison_or_recap` 分支（与 fact_check/claim_audit 同级 `if`）
3. 在 `_WRITER_SYSTEM` 加入 comparison/recap 规则段（参照原文档的 "Comparison / official-recap rules" 字串，可直接采用）
4. `_question_is_comparison` 仅作为 intent 缺失时的兜底，且不再使用与 fact-check 重叠的关键词（去掉 `"official sources say"` 等冲突词）

### 结论

**应修复，但需调整方案。**
- **主修**：`_question_is_fact_check` 显式排除 `official_recap`/`comparison` intent
- **辅修**：按文档加 comparison 模式的 system addendum 与规则段
- **不推荐**：仅按原文档方案修——会出现 fact-check 与 comparison 双触发互斥

---

## Issue 5 — echo_chamber 视觉图为空【可与 Issue 6 合并修复】

### 代码现状

- `agents/kg_analytics.py:473-479` echo_chamber 仅设置 `metrics`，**完全没有填充 `out.nodes`/`out.edges`**
- 对比 `coordinated_groups`（line 399-414）：明确 append `KGNode(community_id=...)` 和 `KGEdge(rel_type="ReplyWithin")`

### 问题真实性

真实——UI `agraph` 拿不到节点/边数据，只能显示数字摘要。文档判定"已知行为，无需修复"。

### 更完善的方案（与 Issue 6 联动）

echo_chamber 完全可以仿照 coordinated_groups，把 partition 成员转为 `KGNode`（`community_id` 作为属性），把社区内/跨社区的边转为 `KGEdge`。这样：

- UI 可视化恢复（解决 Issue 5）
- **同时解决 Issue 6 的语义混淆**：当 `out.nodes` 与 `metrics["node_count"]` 数值一致时，LLM 不会再"看到 314 但 UI 显示 0"

### 结论

**建议合并 Issue 5+6 一起修复。** 仅做 metrics 重命名（文档 Issue 6 方案）治标不治本。

---

## Issue 6 — KG metrics 数字混淆【应修复，但有更优方案】

### 代码现状

`agents/kg_analytics.py` 中至少 4 处使用 `metrics["node_count"]` / `metrics["edge_count"]`：

- `influencer_rank`：290-294
- `bridge_accounts`：340-344
- `coordinated_groups`：415-421
- `echo_chamber`：473-479

`agents/report_writer.py:419-425` 把 `k.metrics` 整体喂给 LLM，无字段级语义说明。`coordinated_groups` 的 metrics 与其 `out.nodes`（仅含 ≥ min_size 社区成员，可能少于 `node_count`）也存在对应不一致。

### 问题真实性

真实。当 echo_chamber 返回 `nodes=[]`、`metrics["node_count"]=314` 时，LLM 误描述为"查询到 314 个节点"，与 UI 卡片"Graph nodes: 0"相矛盾。

### 对原文档提议修复的评估

- 方案 A（重命名为 `analyzed_node_count`）：可行，仅是命名澄清
- 方案 B（在 prompt 加 NOTE）：可行，更直接告知 LLM 不要混淆
- 两方案叠加属于轻量修复，但**未触及根因**：echo_chamber 应该真正返回 nodes/edges

### 更完善的方案

1. **首选**：改 echo_chamber 真正填充 `out.nodes`（partition 成员）和 `out.edges`（社区内/跨社区边）。同样补 `bridge_accounts`（top_k 桥接账号已在 `out.nodes`，但应补充其连接的边）。这样 metrics 与 nodes/edges 数值一致，UI 与 LLM 都不会混淆
2. **次选（无法填充时的兜底）**：保留 metrics 但重命名为 `analyzed_node_count`/`analyzed_edge_count`，并在 `_build_payload` 中按文档加 NOTE
3. **不推荐**：仅改 prompt NOTE 而不改 metrics 键名——LLM 在长 prompt 中不一定遵守注释

### 结论

**应修复。建议方案 1（与 Issue 5 合并）+ 方案 2 作为兜底。**

---

## Issue 7 — 数据目录迁移与启动命令【不适用】

- 原文档使用 `ln -sf` 等 macOS / Linux 命令，本仓库当前在 **Windows** 环境（`platform: win32`）
- Windows 等价：`mklink /D`（目录）/ `mklink`（文件），或直接复制 `data/` 目录
- Streamlit 入口路径已确认为 `ui/streamlit_app.py`（与文档一致）
- 此节是环境笔记，不是代码 patch，无需"打补丁"

---

## 推荐修复优先级（按可执行顺序）

| 优先级 | 任务 | 涉及文件 |
|---|---|---|
| **P0** | Issue 3：3 处 `NULLIF(t.dominant_emotion, '')` → `MAX(NULLIF(...))` | `tools/nl2sql_tools.py`（line 124、223、252） |
| **P1** | Issue 4：`_question_is_fact_check` 排除 `official_recap`/`comparison` intent + 加 comparison 模式 system addendum + system prompt 规则段 | `agents/report_writer.py` |
| **P1** | Issue 5+6 合并：echo_chamber 填充 `out.nodes`/`out.edges`；metrics 键改为 `analyzed_*`；`_build_payload` 加 `returned_entities=` 与 NOTE | `agents/kg_analytics.py` + `agents/report_writer.py` |
| **P2** | Issue 2 精修：仅当 `topic_candidates` 为空时追加默认 topic；对锚定但 Kuzu 缺数据情形保持空 KGOutput | `agents/planner_v2.py` |
| **跳过** | Issue 1：R-OVERVIEW 是有意设计，受现有测试保护 | — |
| **跳过** | Issue 7：环境笔记，非代码 patch | — |

## 验证方案

各项端到端验证：

- **Issue 3**：执行上述 PG 验证命令；运行 `pytest tests/` 确认 NL2SQL 相关测试通过；端到端问 "What is the dominant emotion in Iran-US tension discussions?"
- **Issue 4**：分别问 "Verify claim X"（应走 fact_check 输出 verdict）与 "What does official media say about Y?"（应走 recap 输出 Official Media Recap / Community Perspective 双段结构）
- **Issue 5+6**：问 "How does sentiment differ across discussion clusters?"，UI 应显示节点；LLM 不再写"involving 314 nodes and 187 edges"与"Graph nodes: 0"相互矛盾的话
- **Issue 2**：构造一个 PG 中存在但 Kuzu 中无数据的 topic（可在 fixture 中临时移除某 topic 子图），问与该 topic 相关的图问题，应得到"图数据不足"而非虚假最热 topic 数据
- **Issue 1**：跳过；现有 `tests/test_plan_verifier.py` 是行为契约的来源；任何修改都需要先提出测试改动建议

# Phase 4 完成总结（redesign-2026-05）

> 状态：Phase 4 前台主链路全部就位
> 日期：2026-05-01

## 1. 已交付的模块

### 数据契约
- `models/query.py` —— `Subtask / SubtaskTarget / RewrittenQuery`（含 9 类 SubtaskIntent）
- `models/report_v2.py` —— `ReportV2 / ReportNumber`（替代 v1 IncidentReport for chat）
- `models/session.py` —— ConversationTurn 新增 `branches_used: list[str]`
- `models/chat.py` —— ChatResponse 新增 `branches_used / branch_outputs / citations / needs_human_review`

### Agents
- `agents/query_rewriter.py` —— `QueryRewriter`：1-3 子任务拆分 + 上下文消歧 + 失败降级 → `RewrittenQuery`
- `agents/planner_v2.py` —— `BoundedPlannerV2`：subtask → BranchSet → 并行执行；max 3 并行 / max 5 总步；Q9 置信度规则（命中 ≥ 3 跳过 few-shot）；默认 evidence/nl2sql/kg runner
- `agents/report_writer.py` —— `ReportWriter`：单次 LLM 综合三源 → 带引用的 markdown + ReportNumber 结构化数字；LLM 失败 fallback 到去引擎模板
- `agents/quality_critic.py` —— `QualityCritic`：四轴校验（citation 完整性 / 数字一致性 程序化 + on-topic / hallucination LLM）；LLM 不可达时宽松通过
- `agents/chat_orchestrator.py` —— **重写为 v2 链路**：rewrite → planner → writer → critic（retry 1 次，二次失败标 `needs_human_review`）→ Reflection → Session

### Services / UI
- `services/session_store.py` —— `append_turn` 新增 `branches_used` 参数（向后兼容）
- `services/answer_composer.py` —— 标记 DEPRECATED
- `ui/components/analysis_tabs.py` —— 新增 `route_branches_to_panels(branches_used, branch_outputs)`，旧 `route_capability_to_panels` 留作 fallback
- `ui/pages/0_Chat.py` —— 优先使用 v2 路由，缺字段时降级到 v1

### 测试
- `tests/test_phase4_v2.py` —— 25+ 个测试覆盖 rewriter / planner / writer / critic / orchestrator
- `tests/test_phase{1,2,3}_chat.py` —— 加 `pytestmark = pytest.mark.skip(...)` 标记（v1 链路已废弃，待 v1 capabilities/ 删除时一起删）

## 2. 关键契约（Phase 4 落实）

### 错误归因层级（与 Phase 3 拼接完成）

| 层 | 触发位置 | 归因写到 |
|---|---|---|
| L1 NL2SQL 内部 | `tools/nl2sql_tools.NL2SQLTool._execute` | NL2SQL repair 循环；不进 Reflection |
| L2 Critic | `agents/quality_critic.QualityCritic.review` | `services/reflection_store.ReflectionStore.record`（Phase 3） |

### Critic retry 策略
- 第 1 次失败 → 同 (rq, execution) 重写一次（writer 内部 LLM 因为 temperature=0 输出可能差异不大，但不引入新分支调用，省成本）
- 第 2 次失败 → 标 `report.needs_human_review=True`，notes 追加 `critic_failed:<error_kind>`，仍返回给用户（UI 显示 banner）

### Q9 置信度规则（落实在 Planner v2）
- `count_branch_combo_successes(branches) >= 3`：高置信，跳过 Chroma 3 few-shot
- 否则：召回 top-5 exemplar（advisory；Phase 5 再消费）

### Bounded 上限
- max 3 分支并行 per workflow
- max 5 invocation 总数 across subtasks
- 拒绝调度时直接截断 plan，记 `notes`

### Session 字段升级
- `ConversationTurn.branches_used: list[str]`
- `SessionState.recent_visuals` 暂时保留（Phase 5 决定是否删）

## 3. 流程图（Phase 4 后的完整前台 Pipeline）

```
POST /chat/query
  └─► load session JSON
        └─► QueryRewriter (LLM #1)            -> RewrittenQuery (subtasks)
              └─► BoundedPlannerV2
                    ├─► hybrid_retrieval      (branch A; tools/hybrid_retrieval.py)
                    ├─► nl2sql_tools          (branch B; tools/nl2sql_tools.py)
                    └─► kg_query_tools        (branch C; tools/kg_query_tools.py)
              -> PlanExecutionV2
        └─► ReportWriter (LLM #2)             -> ReportV2
              └─► QualityCritic (LLM #3, optional)
                    ├─► passed -> proceed
                    └─► failed -> retry once
                              └─► failed again -> needs_human_review=True
        └─► ReflectionStore.record            (Chroma 2/3 auto-curate)
        └─► save session, return ChatResponse
              (answer_text + branches_used + branch_outputs + citations)
```

## 4. 已知 / 后续工作

- **Critic LLM 校验当前共 1 次调用**：on-topic + hallucination 在同一 prompt 里输出。够用，但若想区分两类错误的 retry 行为，Phase 5 可拆分。
- **Reflection 的 Ablation runner 仍是 noop**（Phase 3 留的占位）。Phase 5 接 Critic + Planner 注入"重跑不带这条 record"逻辑。
- **v1 `agents/planner.py` + `capabilities/`** 仍在仓库里，因为旧 API endpoint `/capabilities/{name}` 依赖它们。Phase 5 后期统一删。
- **legacy `chat_orchestrator.py` 已重写**。所有引用都得指向 v2；`tests/test_phase{1,2,3}_chat.py` 已 skip，无破坏。
- **UI 部分功能在 v2 下短暂退化**：v2 的 Visual tab 暂时没有数据来源（v1 visual_summary 已 deprecated）。后续可改为展示 KG 路径图或 evidence top chunks 缩略。

## 5. 本次结尾的 LLM 调用上限

每次 Chat 调用最多 3 次 LLM：
1. Query Rewriter
2. Report Writer
3. Quality Critic（可跳过；同一次调用覆盖 on-topic + hallucination）

平均期望延迟：rewriter ~0.6s + 三分支并行 ~2-5s + writer ~1.5s + critic ~0.8s ≈ **5-8s**。

## 6. 下一步

进入 Phase 5：Reflection 看板 + 评分/衰减 + Ablation runner 真实化 + 旧 capabilities/v1 planner 清理。

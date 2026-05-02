# Phase 1 依赖审计

> 本文档记录 redesign v2 Phase 1 启动前对 v1 模型 / 模块依赖的扫描结果，作为后续删除决策的依据。
> 生成日期：2026-05-01

## 1. 关键 v1 模型的引用面

通过 `grep "from models\.claim|from models\.report import.*Claim|InterventionDecision|CounterMessage|from models\.risk_assessment"` 扫描得到 22 处引用：

### 1.1 文档（不影响代码）
- `PROJECT_REDESIGN_V2.md`
- `README.md`

### 1.2 v1 主干（必须保留以维持 v1 兼容）
- `agents/__init__.py`
- `agents/precompute_pipeline.py`
- `agents/knowledge.py`
- `agents/risk.py`
- `agents/critic.py`
- `agents/counter_message.py`
- `agents/visual.py`
- `agents/report.py`
- `models/report.py`
- `services/metrics_service.py`
- `services/intervention_decision_service.py`
- `services/actionability_service.py`
- `services/cli.py`
- `main.py`

### 1.3 前台 v1 链路
- `capabilities/explain_decision_capability.py`
- `capabilities/visual_summary_capability.py`
- `tools/decision_tools.py`
- `tools/visual_tools.py`

### 1.4 测试
- `tests/test_functional.py`
- `tests/test_phase3_chat.py`

## 2. Phase 1 处理策略

| 文件 | 状态 | 说明 |
|---|---|---|
| 1.2 v1 主干 | **保留 + 加 deprecated 注释** | v1 仍可用，标记后续清理 |
| 1.3 前台 v1 链路 | **保留** | 留到 Phase 4 删 capabilities/ 时统一处理 |
| 1.4 测试 | **保留** | 留到 Phase 4 末统一重写 |

## 3. v2 不会引入的模型

下面这些 v1 模型在 v2 链路里**不再使用**：
- `models.claim::Claim` / `ClaimEvidence`
- `models.report::IncidentReport.claims` / `InterventionDecision` / `CounterMessage`
- `models.risk_assessment::RiskLevel`
- `models.immunity::ImmunityStrategy`
- `models.persuasion::CascadePrediction` / `PersuasionFeatures` / `CounterTargetPlan` / `CounterTargetRec`

v2 新增的 Pydantic 模型：
- `models.entity::EntitySpan`
- `models.official_chunk::OfficialChunk`（暂时复用 `data/official_chunks/*.jsonl` 存储，Phase 2 落 Chroma 1）

## 4. v1 / v2 并存机制

`main.py` 增加 `--pipeline {v1,v2}` 开关，默认 `v1` 不动现有行为。v2 走新 `agents/precompute_pipeline_v2.py`。

旧测试只跑在 v1 上；新测试 `tests/test_phase1_v2.py` 只跑 v2，互不干扰。

## 5. 已知风险

- `agents/__init__.py` import 了所有 v1 agent（counter_message / visual / critic 等）。Phase 1 删除其内容会导致 ImportError 影响整个 `agents` 包。**对策**：Phase 1 不动 `__init__.py`，只在被 deprecated 的文件里加文件头注释。
- `tests/test_functional.py` 直接 import `Claim`。Phase 1 不动模型，等 Phase 4 重写。

## 6. 下一步

进入 Phase 1.2：新建 `agents/precompute_pipeline_v2.py`（与 v1 并存）。

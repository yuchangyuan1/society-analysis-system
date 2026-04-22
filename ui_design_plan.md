# 社会分析 AI Agent 交互式项目 UI 设计方案

## 1. 设计目标

本项目的 UI 不应被设计成单纯的仪表盘，也不应只是一个套壳聊天框，而应定位为一个 **Chat-first 的分析工作台**：用户可以通过自然语言提出问题，系统按意图动态调用相应能力，并在同一界面中展示证据、传播结构、指标与图卡输出。

UI 需要同时满足以下四个目标：

1. **可提问**：支持用户以自然语言直接发起分析任务，而不是先跑完整 pipeline 再看结果。
2. **可解释**：每条回答都能追溯到 run、topic、claim、evidence 和 graph。
3. **可下钻**：用户可以从热点概览进入单个 topic、claim 或传播路径的深层分析。
4. **可答辩**：界面要明显体现“Router + Capability + Tool + Data Layer”的交互式 Agent 架构，而不是静态 artefact 浏览器。

---

## 2. 产品定位

### 2.1 UI 定位

建议将产品定义为：

> **社会分析 AI Agent 工作台（Social Analysis Agent Workspace）**

而不是：

- 纯 Dashboard
- 纯聊天机器人
- 纯报告查看器

### 2.2 设计原则

1. **Chat-first**：聊天是主入口。
2. **Workspace-oriented**：分析结果要在工作区内可继续操作，而不是一次性吐出长文。
3. **Explainable by default**：每条结论默认带来源、状态、上下文。
4. **Minimal execution path**：按用户问题动态编排最小必要工作流，不默认跑全链路。
5. **Run-aware**：所有分析必须绑定到具体 run / 时间窗 / 数据范围。

---

## 3. 信息架构（Information Architecture）

建议采用以下页面结构：

### 3.1 主页面：Chat Workspace

这是用户的主要交互入口，用于：

- 提问
- 查看 Agent 回答
- 下钻到 topic / claim / evidence / graph
- 触发图卡生成

### 3.2 辅助页面：Run Explorer

用于查看已有 run 的结构化结果，承接原有 run-centric 系统资产：

- run 列表
- metrics 摘要
- report / visuals / manifests
- baseline run 与 live run 对比

### 3.3 深入页面：Topic Detail View

用于对单个 topic 进行深度分析：

- topic summary
- 情绪构成
- primary claim
- claim 状态
- evidence 细节
- propagation / social graph
- 图卡生成与预览

### 3.4 可选页面：Run Compare View

用于对比两个 run 的：

- 热点变化
- 情绪变化
- claim 结构变化
- 干预结论变化

---

## 4. 主界面布局方案

建议采用 **三栏式布局**：

- 左侧：上下文与筛选区
- 中间：Chat 主区
- 右侧：分析工作区

### 4.1 左侧：Context & Filters Sidebar

#### 功能内容

1. **数据范围选择**
   - Community / subreddit
   - 时间窗（today / 3 days / 7 days / custom）
   - run selector（latest / saved run）

2. **当前会话上下文**
   - 当前 selected topic
   - 当前 selected claim
   - 当前 compared runs
   - 当前 filters

3. **快捷操作入口**
   - 热点话题
   - 情绪概览
   - claim 状态
   - 传播结构
   - 图卡生成

4. **会话 breadcrumb**
   - 当前问题链路
   - “你现在正在分析：Run X > Topic Y > Claim Z”

#### 设计建议

- 宽度控制在 240–280px
- 默认固定显示，移动端可折叠
- 使用轻量标签而不是复杂树形菜单

### 4.2 中间：Chat Workspace

这是系统的核心区域。

#### 顶部区域

- 一个主输入框
- 示例 prompt chips，例如：
  - 帮我分析今天最热的 3 个话题
  - 哪些 claim 证据不足？
  - 这个 topic 的传播结构是怎样的？
  - 帮我生成一张主题总结图

#### 聊天消息流

消息流应包含两类卡片：

1. **用户消息卡**
2. **Agent 回答卡**

#### Agent 回答卡建议结构

1. **Header**
   - 回答标题
   - 使用能力标签（Topic / Emotion / Claim / Propagation / Visual）
   - 当前分析范围（run / topic / claim）

2. **Main Answer**
   - 2–5 句自然语言摘要

3. **Structured Findings**
   - 热点 top-k
   - 情绪主导类型
   - 证据状态
   - actionability / abstention 状态
   - 推荐下一步

4. **Traceability Footer**
   - 数据来源
   - 更新时间
   - 使用的 capability / tools

5. **Follow-up Actions**
   - 查看证据
   - 查看传播路径
   - 打开 topic 详情
   - 生成图卡
   - 对比上一轮 run

#### 交互建议

- 每条回答后都附 2–4 个推荐追问按钮
- 支持点击卡片中的 topic / claim 标签触发上下文切换
- 长结果默认折叠，保留“展开更多”

### 4.3 右侧：Analysis Workspace Panel

右侧不建议固定放一张图，而应设计为 **tabbed analysis workspace**。

建议包含以下标签页：

#### Tab A：Evidence

展示：

- supporting / contradicting / uncertain evidence
- source 名称
- snippet / summary
- stance
- confidence / coverage
- 链接到原始来源

#### Tab B：Topic / Claim Detail

展示：

- topic summary
- claim list
- primary claim
- claim_actionability
- non_actionable_reason
- intervention decision
- recommended next step

#### Tab C：Propagation / Social Graph

展示：

- topic graph 可视化
- 角色分布（originator / amplifier / bridge / passive）
- bridge accounts
- community 分裂结构
- propagation summary

#### Tab D：Metrics

展示：

- emotion share
- topic volume
- claim count
- evidence coverage
- bridge_influence_ratio
- role_risk_correlation
- modularity_q

#### Tab E：Visual Card Preview

展示：

- rebuttal card
- evidence-context card
- abstention explanation card（如保留视觉化）
- regenerate / export 按钮

---

## 5. 关键用户流（User Flows）

### 5.1 Flow A：热点概览 → topic 下钻

1. 用户提问：今天有哪些热点话题？
2. Router 识别为 Topic Overview
3. 中间聊天区返回 top topics 摘要
4. 用户点击某个 topic
5. 右侧切换到 Topic Detail / Metrics / Evidence
6. 用户继续问：这个 topic 情绪如何？

### 5.2 Flow B：claim 判断 → 证据查看 → abstain/rebut explain

1. 用户提问：哪些讨论可能有问题？
2. Router 调用 Claim Status Capability
3. 返回 claim 状态列表
4. 用户点击某个 claim
5. 右侧展开 Evidence tab
6. 若证据不足，则展示 insufficient evidence / abstention explanation
7. 若可反制，则展示 rebuttal summary 和图卡生成入口

### 5.3 Flow C：传播分析 → 图谱解释

1. 用户提问：这个 topic 是怎么传播开的？
2. Router 调用 Propagation Insight
3. 返回文本解释 + 右侧 graph tab
4. 用户在 graph 中点击账号或社区节点
5. 右侧显示角色、连接关系和简短说明

### 5.4 Flow D：生成图卡

1. 用户提问：帮我把这个主题做成一张图
2. Router 调用 Visual Summary Capability
3. 右侧 Visual tab 显示图卡预览
4. 中间回答区补充生成依据与适用场景

---

## 6. 组件设计建议

### 6.1 顶层页面组件

- `AppShell`
- `SidebarContextPanel`
- `ChatWorkspace`
- `AnalysisTabsPanel`
- `RunExplorerPage`
- `TopicDetailPage`
- `CompareRunsPage`

### 6.2 聊天与回答组件

- `ChatInput`
- `PromptSuggestions`
- `UserMessageBubble`
- `AgentAnswerCard`
- `AnswerHeader`
- `CapabilityBadgeGroup`
- `StructuredFindingsList`
- `FollowupActionButtons`

### 6.3 分析工作区组件

- `EvidenceTable`
- `ClaimStatusPanel`
- `TopicSummaryCard`
- `PropagationGraphPanel`
- `MetricsSummaryPanel`
- `VisualPreviewPanel`

### 6.4 全局状态组件

- `RunSelector`
- `TimeWindowSelector`
- `TopicPill`
- `ClaimPill`
- `SessionBreadcrumb`
- `ExecutionStatusBar`

---

## 7. 页面详细设计

### 7.1 Chat Workspace 页面

#### 布局建议

- 顶部：导航 + run selector + community selector
- 左侧：上下文栏
- 中间：聊天区
- 右侧：分析区

#### 关键状态

- empty state：显示示例问题和最近 run
- loading state：显示“Router 正在识别意图 / Capability 正在执行”
- partial result state：先返回摘要，再逐步填充 evidence / graph / metrics
- error state：显示失败模块和建议重试动作

#### 空状态文案建议

- “从热点、情绪、claim、传播或图卡生成开始提问”
- “示例：帮我总结今天社区里最热的 3 个话题”

### 7.2 Run Explorer 页面

#### 页面内容

- 左侧：run 列表
- 右侧：run summary
  - community
  - time range
  - posts / claims / topics 数量
  - modularity_q
  - intervention decision
  - visuals list

#### 交互动作

- 打开该 run 进入 Chat Workspace
- 设为当前上下文
- 对比另一轮 run

### 7.3 Topic Detail 页面

#### 页面内容

- topic 标题与 summary
- emotion distribution
- claims list
- evidence coverage
- social graph snapshot
- visual card generation section

#### 页面价值

- 适合答辩时展示“一个 topic 从识别到解释再到可视化输出”的完整闭环

---

## 8. 状态管理设计

建议将 UI 状态拆成三层：

### 8.1 Session State

用于保存会话级上下文：

- current_run_id
- selected_topic_id
- selected_claim_id
- selected_time_window
- selected_community
- recent_questions

### 8.2 UI View State

用于保存当前视图状态：

- active_right_tab
- expanded_message_id
- graph_selected_node
- visual_preview_mode
- compare_mode_on

### 8.3 Execution State

用于展示 Agent 执行过程：

- routed_capability
- tool_calls_in_progress
- partial_results_ready
- error_source
- retry_available

---

## 9. 视觉设计建议

### 9.1 风格关键词

- 专业
- 简洁
- 可解释
- 学术/研究型
- 不做过度营销风

### 9.2 色彩建议

建议按功能而不是装饰用色：

- 蓝色：系统结构 / topic / 中性信息
- 绿色：supporting / grounded evidence
- 红色：contradicting / 高风险提示
- 黄色/橙色：insufficient evidence / abstention
- 紫色：session / context / orchestration

### 9.3 信息密度控制

- 中间聊天区尽量轻量
- 右侧分析区承担高密度信息
- 不在单个卡片中塞入过多图表

### 9.4 图表建议

推荐使用：

- bar / stacked bar：emotion share、topic volume
- simple node-link graph：social graph snapshot
- badge + small cards：claim status、decision state
- table：evidence list、run list

避免：

- 过于复杂的 Sankey / chord diagram 作为默认图
- 全页面 3D 或过度动画

---

## 10. 交互细节规范

### 10.1 回答卡必须包含的字段

每条 Agent 回答建议至少包含：

- 回答标题
- 自然语言摘要
- 当前范围（run/topic/claim）
- capability 标签
- 至少一个 follow-up action

### 10.2 claim 状态展示规范

claim 不应简单二分类成“真/假”，建议使用状态标签：

- Supported
- Contradicted
- Mixed / Uncertain
- Insufficient Evidence
- Non-factual Expression
- Abstained

### 10.3 图卡输出规范

生成视觉图卡时，UI 中应显示：

- 图卡类型
- 使用依据（哪个 topic / claim / evidence）
- 生成时间
- 可导出选项

### 10.4 可追溯性规范

任何结论都至少能追溯到：

- run
- topic 或 claim
- evidence 或 graph
- capability / tool 调用路径（简略显示即可）

---

## 11. MVP 版本建议

如果时间有限，建议优先做如下 MVP：

### 必做页面

1. `Chat Workspace`
2. `Run Explorer`

### 必做能力映射

1. Topic Overview
2. Emotion Insight
3. Claim Status
4. Propagation Insight
5. Visual Summary（可先做简化版）

### 必做 UI 组件

- ChatInput
- AgentAnswerCard
- RunSelector
- TopicSummaryCard
- EvidenceTable
- MetricsPanel
- VisualPreviewPanel

### 可延后

- Compare Runs 专门页面
- 高级 graph 交互
- 多图并排比较
- 移动端深度适配

---

## 12. 前后端对接建议

### 12.1 API 建议

建议前端至少依赖以下接口：

- `POST /chat/query`
- `GET /runs`
- `GET /runs/{run_id}`
- `GET /runs/{run_id}/topics`
- `GET /topics/{topic_id}`
- `GET /claims/{claim_id}`
- `GET /topics/{topic_id}/graph`
- `POST /visuals/generate`

### 12.2 返回结构建议

聊天响应建议返回：

- natural_language_answer
- structured_findings
- context
- active_capability
- followup_actions
- side_panel_payload

这样前端可以同步更新中间聊天区与右侧分析区。

---

## 13. 答辩演示建议

最推荐的 demo 路线如下：

1. 用户提问：今天社区里最热的 3 个话题是什么？
2. 系统展示热点概览
3. 用户点击某个 topic，查看情绪与 claim 状态
4. 用户追问：这个主题哪些内容证据不足？
5. 系统解释为何 abstain / insufficient evidence
6. 用户追问：帮我生成一张主题总结图
7. 系统输出 visual card，并展示依据

这条 demo 路线能完整体现：

- Router based orchestration
- capability invocation
- explainability
- topic drill-down
- visual output

---

## 14. 最终建议

本项目 UI 的最佳形态，不是“大屏式展示”，也不是“纯对话机器人”，而是：

> **以聊天为入口、以工作区为核心、以证据和图谱为解释支撑的交互式分析系统。**

因此最终推荐的 UI 方案为：

- **主页面采用三栏式 Chat Workspace**
- **右侧使用多标签分析工作区**
- **保留 Run Explorer 作为 run-centric 资产承接页**
- **以 topic / claim / evidence / graph / visual 为主要交互对象**
- **确保每一条回答都可追溯、可继续追问、可切换视角**

这套 UI 方案既符合你当前项目的架构改造方向，也便于课程展示和后续扩展。

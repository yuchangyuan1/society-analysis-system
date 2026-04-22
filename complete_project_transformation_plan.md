# Complete Project Transformation Plan
**Interactive Social Analysis AI Agent Refactor Plan**

## 1. Objective

This refactor is intended to transform the current system from a **run-centric offline reporting pipeline** into a **query-centric interactive AI Agent** that can answer user questions such as:

- What are the hot topics discussed today?
- How is a specific topic spreading across the community?
- What is the emotional profile of a topic?
- Is a topic or claim supported, contradicted, or insufficiently evidenced?
- Can official sources be provided to support or challenge a claim?
- Can the system summarize a topic in a visual card?

The new system should allow a user to ask a question in the UI, after which the system:
1. identifies the intent,
2. extracts the relevant parameters,
3. plans a bounded workflow,
4. invokes the necessary subagents/tools,
5. retrieves graph and evidence data,
6. returns a grounded and explainable answer.

---

## 2. High-level decision

The project should **not** discard the existing work.  
It should also **not** continue pretending that the current chat layer is a real planner.

### Final decision
The target system will be:

> **Offline analysis backbone + online query planner + composable subagents + Kuzu-backed graph queries + evidence-grounded on-demand answering**

This means:
- Keep the existing precompute pipeline as the backend analytical base
- Rebuild the query path
- Rename misleading modules
- Split batch and on-demand computation clearly
- Refactor current capabilities into planner-callable subagents
- Use Kuzu as the canonical graph backend
- Add scheduled refresh
- Keep true MCP protocolization out of scope for the course milestone

---

## 3. What to keep

### 3.1 Keep the offline analytical backbone
The current 24-stage pipeline still provides substantial value and should remain the backbone for heavy offline computation.

Keep:
- scraping
- cleaning / deduplication
- claim extraction
- topic clustering
- community detection
- role classification
- graph construction
- run metrics
- report artifacts
- baseline visual outputs

### 3.2 Keep current analytical logic
The logic already implemented for:
- topic analysis
- emotion analysis
- claim extraction
- propagation/community analysis
- evidence collection
- reporting / visualization

should be reused rather than rewritten from zero.

### 3.3 Keep run artifacts
Artifacts such as:
- `report_raw.json`
- `metrics.json`
- sample runs
- fixed fixtures
- visual outputs

should remain for:
- demo reproducibility
- debugging
- regression checks
- answer caching
- instructor presentation

### 3.4 Keep official-source ingestion
The existing official-source collection work should remain and evolve into a stronger evidence layer.

### 3.5 Keep visual generation capability
Existing card generation should not be thrown away.  
It should be converted from a batch output into a planner-triggered capability.

---

## 4. What must change

### 4.1 Rename the current planner
The current `agents/planner.py` is a batch pipeline orchestrator, not a user-query planner.

### Required change
- `agents/planner.py` -> `agents/precompute_pipeline.py` or `agents/offline_orchestrator.py`

### Why
This removes semantic confusion and frees the name `Planner Agent` for the actual online planner.

---

### 4.2 Introduce a real Planner Agent
A new planner must be introduced for query-time orchestration.

### Responsibilities
- read the structured intent output
- inspect available subagent manifests
- choose a bounded workflow template
- bind parameters
- sequence calls
- merge intermediate outputs
- return a final answer plan to the answer composer

### Important scope choice
This planner will **not** be a fully open-ended autonomous agentic loop in the course-project version.

### Final choice
The planner should use:

> **single-shot bounded DAG planning**

and **not** a fully open-ended agentic loop.

### Reason
This is better for:
- reproducibility
- latency control
- debugging
- evaluation
- course-demo stability

---

### 4.3 Split Router and Planner
The system should separate lightweight intent recognition from workflow planning.

### Router Agent
Responsible for:
- intent classification
- topic / claim / time range extraction
- output preference extraction
- recognizing whether graph / evidence / visual output is requested

### Planner Agent
Responsible for:
- selecting workflow templates
- choosing subagents/tools
- defining the execution order
- deciding whether retrieval, graph query, comparison, or visual generation are necessary

---

### 4.4 Refactor capabilities into planner-callable subagents
Current capabilities are too closed and final-output-oriented.

They should be converted into reusable, planner-callable components with:
- manifest metadata
- input schema
- output schema
- intermediate artifact support

### Target capability families
- Topic Overview
- Emotion Insight
- Propagation Insight
- Claim Verification
- Evidence Retrieval / Comparison
- Visual Summary
- Run Comparison / Metrics Readout

---

### 4.5 Re-split batch and on-demand execution
This is the most important architectural change.

The current system pushes too much work into offline precomputation and leaves the chat path mostly reading existing artifacts.

The refactor should explicitly separate:

## Batch / scheduled work
Keep offline:
- community scraping
- cleaning / deduplication
- entity extraction
- claim extraction and normalization
- topic clustering
- community / role detection
- graph updates into Kuzu
- embedding refresh
- run store update
- baseline summary generation

## On-demand work
Move to query-time:
- topic-focused summarization
- claim verdict explanation
- evidence retrieval for a specific user question
- official-vs-community comparison
- propagation explanation
- visual generation for the requested topic/claim
- answer synthesis

---

## 5. Claim verdict design

This point must be explicit.

The system should **not** make claim verdict fully offline, and should also **not** make everything fully online from scratch.

### Final decision
Use a **hybrid claim-verdict design**.

### Batch side
Precompute:
- candidate claims
- normalized claims
- topic-to-claim linking
- post-to-claim mapping
- cached evidence candidates where useful

### On-demand side
At query time perform:
- official evidence retrieval
- relevant community-post retrieval
- support / contradiction / insufficient-evidence judgment
- answerable explanation generation
- source-grounded verdict wording

### Why
This balances:
- latency
- cost
- freshness
- explainability
- query specificity

---

## 6. Graph strategy

### Final decision
Use **Kuzu as the canonical graph backend**.

The current path that rebuilds `NetworkX` graphs from `report_raw.json` should no longer be the primary online graph access path.

### New principle
- **Kuzu** = canonical graph storage and query backend
- **NetworkX** = optional fallback / debug / local visualization helper only

### Required change
`tools/graph_tools.py` should be rewritten to query Kuzu directly for online graph lookups.

### Why
This makes the online system consistent with the intended architecture and avoids maintaining two competing graph semantics.

---

## 7. Evidence layer and RAG design

The new system should not treat RAG as “official source retrieval only.”

For user questions like:
- Is this rumor true?
- What does official evidence say?
- Can you compare what the community says versus official sources?

the system must retrieve both:

1. **official / credible evidence**
2. **relevant community posts / claims**

### Final design
The evidence layer should support:
- official source retrieval
- community-post retrieval
- claim-to-evidence comparison
- evidence-grounded answer generation

### Recommended storage split
- vector / document retrieval layer for official evidence and post chunks
- Kuzu for structural relations across topic / claim / post / account / community / entity

### Principle
- **RAG handles semantic evidence retrieval**
- **Kuzu handles structural relations**
- **Planner combines them**

---

## 8. Scheduler design

A scheduler should be added for regular refresh.

### Scheduled tasks
- scrape community data
- collect or refresh official-source corpus
- run embeddings
- refresh graph state
- generate or update run store

### Recommended implementation
Use a lightweight scheduler such as:
- APScheduler
- cron
- GitHub Actions cron

### Note
A full workflow-orchestration platform is not required for the course milestone.

---

## 9. Naming and folder decisions

This section is important because it addresses earlier ambiguity directly.

### 9.1 Existing `skills/` folder
The existing `skills/` folder already contains Claude-style markdown skills.

### Final rule
Do **not** place new executable Python subagents/tools into the existing `skills/` directory.

### Reason
That would mix:
- markdown skill instructions
- executable planner-callable modules

which would create unnecessary structural confusion.

### Recommended new locations
Use one of:
- `subagents/`
- `capabilities/`
- `agent_tools/`

for executable components.

---

### 9.2 MCP terminology
The project should **not** claim real MCP protocol support unless it actually implements:
- an MCP server
- transport layer
- tool schema registration under the MCP protocol
- proper client/server invocation

### Final scope decision
For the current course-project phase:

> The project will use **internal registered tools/subagents**, not full protocol-level MCP.

### Therefore
The design should avoid overstating “MCP” unless it is explicitly framed as:
- internal tool registry
- MCP-inspired modularization
- future protocolization path

### Safer wording
Use:
- “planner-callable tools”
- “registered subagents”
- “internal tool registry”

instead of claiming finished real-MCP implementation.

---

## 10. UI and interaction model

The UI should support user-driven question answering rather than just run browsing.

### Required front-end behavior
The user should be able to:
- type a natural-language question
- view the planner-selected workflow result
- inspect evidence and citations
- inspect graph / topic / metric panels
- request a follow-up visual summary
- ask follow-up questions in session context

### Core answer outputs
The system should be able to return:
- concise text answer
- evidence table
- official-source citations
- topic summary
- propagation explanation
- visual card
- graph snapshot
- metric panel

### Session memory
The chat session should maintain short-lived interaction context such as:
- selected run
- selected topic
- selected claim
- previously retrieved evidence
- prior answer branch

This avoids forcing the user to restate the same topic repeatedly.

---

## 11. Recommended query workflow templates

The planner should choose from bounded workflow templates, not invent arbitrary flows.

### Example workflow templates
- `topic_overview_flow`
- `emotion_insight_flow`
- `propagation_analysis_flow`
- `claim_verification_flow`
- `evidence_comparison_flow`
- `visual_summary_flow`
- `run_comparison_flow`

### Example: Topic overview
1. identify run scope
2. retrieve hot topics
3. fetch topic summary and topic graph context
4. return ranked answer and optional chart

### Example: Propagation analysis
1. resolve target topic
2. query Kuzu graph
3. identify relevant accounts / communities / paths
4. summarize spread pattern
5. optionally show graph visualization

### Example: Claim verification
1. resolve target topic/claim
2. retrieve relevant community claims
3. retrieve official evidence
4. compare claim vs evidence
5. produce verdict explanation
6. return citations and optional visual card

---

## 12. Performance and latency assumptions

This section should be explicit so the architecture remains grounded.

### Working assumption
The course-project version does **not** require sub-5-second responses for all question types.

### Acceptable latency target
For complex queries:
- ~10–30 seconds is acceptable
- up to ~30–60 seconds may still be acceptable for heavy evidence-comparison workflows in demo settings

### Consequence
This supports:
- bounded DAG planning
- multi-step evidence retrieval
- query-time comparison logic

without forcing oversimplification.

---

## 13. Freshness assumption

The system should explicitly accept a scheduled refresh model.

### Working assumption
Daily refresh is acceptable for most “today’s discussion” style analysis in the course-project context.

### Interpretation
If the data was refreshed in the early morning, user questions later in the day may still operate on several-hours-old data.

This is acceptable if clearly documented in the UI and demo framing.

---

## 14. Proposed phased implementation

## Phase 0 — structural cleanup
- rename current planner module
- define Router / Planner split
- freeze folder naming (`subagents/`, not `skills/`)
- document that current project phase is not full MCP protocolization
- define batch vs on-demand boundary
- define claim-verdict hybrid strategy

## Phase 1 — online orchestration core
- implement Router Agent
- implement Planner Agent with bounded DAG planning
- create subagent manifest schema
- refactor existing capabilities into planner-callable subagents
- create answer-composition contract

## Phase 2 — data/query backend alignment
- rewrite graph query path to Kuzu
- expose topic / claim / propagation / community lookup services
- add evidence comparison service
- connect official evidence retrieval with community-post retrieval

## Phase 3 — scheduled refresh and caching
- add scheduler
- create refresh/update jobs
- define run-selection and cache strategy
- maintain reusable run store for online answering

## Phase 4 — UI interaction layer
- build interactive UI flow around user queries
- add evidence panel / graph panel / visual panel / metrics panel
- support topic and claim selection
- support follow-up questioning within a session

---

## 15. What is explicitly out of scope for this milestone

To keep the refactor bounded, the following are out of scope for the course milestone unless time remains:

- full protocol-level MCP server implementation
- fully autonomous open-ended agentic loop
- arbitrary self-reflective planning with unlimited tool retries
- perfect real-time data freshness
- production-grade multi-user deployment
- complete replacement of all offline computations with live computation

This keeps the redesign implementable and defensible.

---

## 16. Final architectural statement

The final project should be described as follows:

> The system consists of an offline social-analysis backbone that continuously ingests and structures community discussions, and an online interactive AI Agent layer that plans bounded workflows on demand to answer user questions using graph queries, evidence retrieval, and explainable synthesis.

More concretely:

- offline handles **data preparation and structured analysis**
- online handles **question understanding and on-demand reasoning**
- Kuzu provides **canonical graph access**
- retrieval provides **official and community evidence**
- the planner coordinates **bounded multi-step workflows**
- the UI exposes the system as a real **interactive social-analysis AI Agent**

---

## 17. Final summary: keep / change / add

### Keep
- current batch pipeline backbone
- current analysis logic
- run artifacts
- official-source ingestion
- visualization foundation

### Change
- misleading planner naming
- run-centric chat path
- fixed capability routing
- report-file-based graph access
- overly offline verdict logic

### Add
- real Router Agent
- real Planner Agent
- bounded DAG workflow planning
- planner-callable subagents
- Kuzu-backed online graph query path
- hybrid claim-verdict workflow
- scheduler
- interactive UI/session layer

---

## 18. Final recommendation

The correct course-project refactor is **not** “patch the existing chat layer a bit more.”

It is:

> **Keep the current offline pipeline as the analytical substrate, then build a new query-time orchestration layer with Router + Planner + subagents + Kuzu-backed graph access + evidence comparison.**

That direction:
- answers the architectural mismatch,
- preserves prior work,
- aligns with the intended user experience,
- avoids misleading MCP claims,
- and creates a coherent path from the current system to the intended interactive AI Agent.

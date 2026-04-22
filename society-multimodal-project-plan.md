# Society Project Plan

## 1. Project Positioning

### Proposed Title

**Multimodal Social Media Propagation Analysis and Counter-Messaging System**

### One-Sentence Goal

Build a multi-agent system that ingests social-media-style text and image posts, tracks the spread of claims and topics, assesses misinformation and influence risk, and produces evidence-backed reports plus counter-messaging content.

### Why This Fits "Society"

This project aligns with the course's Society direction because it focuses on:

- information dissemination in context
- social-media-scale topic and claim tracking
- misinformation and persuasive messaging detection
- social and community-level propagation analysis
- counter-messaging and intervention support

This is a strong fit for the course's Society direction.

### Platform and Language Scope

- **Target Platform**: X (Twitter/X) — primary data source via X API v2
- **Primary Language**: English (US-focused project)
- **Implication**: All OCR, embedding, claim normalization, and counter-messaging components are configured for English-first operation.

## 2. Problem Definition

The system should answer questions such as:

- Is a specific claim currently spreading across a set of social posts?
- What evidence supports or contradicts that claim?
- Is the propagation pattern suspicious, misleading, or coordinated?
- What concise counter-message should be published?
- Can the system generate a visual clarification card for that counter-message?

## 3. Design Principles

### Framework: OpenClaw

This project is built on **OpenClaw** (v2026.4.x, MIT license, GitHub: openclaw/openclaw).

OpenClaw is chosen for three specific capabilities that directly match this project's needs:

| OpenClaw Capability | How It Is Used Here |
|---|---|
| **Workspace isolation** | Each subagent (ingestion, analysis, risk, critic, visual) runs in its own workspace; file system state and auth credentials are fully isolated |
| **Skills system** | Each reusable tool-calling rule (`claim-retrieve`, `image-post-ingest`, etc.) is implemented as an OpenClaw skill (`SKILL.md` + service call) |
| **Multi-agent routing** | The planner agent routes user intent to the appropriate subagent workspace via OpenClaw's built-in routing |

OpenClaw's daemon model is used in trigger mode for this analytical pipeline — agents are invoked on demand rather than running as persistent messaging bots.

### Architecture Principles

- Build as a greenfield project using OpenClaw as the agent runtime framework.
- Keep deterministic execution in services; agentic reasoning stays inside bounded stages.
- Use OpenClaw skills as the standard tool-calling contract.
- Use the planner agent as the orchestration and routing layer.
- Use hard orchestration for the main workflow and soft agentic reasoning inside bounded steps.

### Safety Principles

- No strong conclusion without evidence.
- Conflicting evidence must be disclosed.
- High-risk or low-evidence outputs must require human review.
- Generated counter-messaging must pass critic review before release.

## 4. High-Level Architecture

```mermaid
flowchart LR
    U["User / Analyst"] --> P["Planner Agent\n(OpenClaw main workspace)"]

    subgraph OC["OpenClaw Runtime"]
        P -->|multi-agent routing| WI["Ingestion Workspace"]
        P -->|multi-agent routing| WK["Knowledge Workspace"]
        P -->|multi-agent routing| WA["Analysis Workspace"]
        P -->|multi-agent routing| WR["Risk Workspace"]
        P -->|multi-agent routing| WG["Counter-Message Workspace"]
        P -->|multi-agent routing| WC["Critic Workspace"]
        P -->|multi-agent routing| WRP["Report Workspace"]
        P -->|multi-agent routing| WV["Visual Workspace"]
    end

    WI -->|skills| SK["OpenClaw Skills\n(x-post-ingest, image-post-ingest, ...)"]
    WK -->|skills| SK
    WA -->|skills| SK
    WR -->|skills| SK
    WG -->|skills| SK
    WC -->|skills| SK
    WRP -->|skills| SK
    WV -->|skills| SK

    WI --> PG["Postgres"]
    WI --> FS["Raw Media Store"]
    WI --> KG["Kuzu Knowledge Graph"]
    WI --> CH["Chroma"]

    WK --> PG
    WK --> CH
    WK --> KG

    WA --> PG
    WA --> KG

    WR --> PG
    WR --> KG

    WRP --> PG
    WC --> PG
    WV --> FS

    WRP --> OUT["Incident Report / Counter-Message / Visual Card"]
    WC --> OUT
```

## 5. System Components

This is a greenfield project built on OpenClaw. Each component maps to an **OpenClaw workspace** with its own isolated file system, memory, and skill set.

| OpenClaw Workspace | Role | Key Skills |
|---|---|---|
| `planner` (main) | orchestration, intent classification, multi-agent routing | — |
| `ingestion` | fetch posts from X API v2, ingest images and articles | `x-post-ingest`, `image-post-ingest` |
| `knowledge` | evidence retrieval, claim deduplication, evidence pack assembly | `claim-retrieve` |
| `analysis` | propagation trend computation, anomaly detection | `propagation-analyze` |
| `risk` | misinformation risk scoring, human review routing | `misinfo-risk-review` |
| `report` | incident report and propagation summary generation | `campaign-report-build` |
| `critic` | evidence sufficiency check, overclaim detection, output gating | `critic-review` |
| `visual` | visual clarification card generation via Stable Diffusion | `counter-visual-generate` |

## 6. Core Agents and Responsibilities

### Planner

- classify user intent
- select the workflow
- trigger skills in the correct order
- aggregate outputs into the final response

### Knowledge

- retrieve relevant posts, articles, fact checks, and prior reports
- build evidence packs
- normalize claim wording
- group evidence by support / contradiction / uncertainty
- resolve claim identity via two-stage deduplication:
  1. **Stage 1 — Embedding similarity** (fast filter): compute cosine similarity against existing claims in Chroma; score ≥ 0.92 → candidate match; score < 0.85 → new claim
  2. **Stage 2 — LLM judgment** (precision check): submit candidate pair to LLM for semantic equivalence decision; output `SAME` / `RELATED` / `DIFFERENT`
     - `SAME` → merge nodes, increment propagation count
     - `RELATED` → add `related_to` edge, keep separate nodes
     - `DIFFERENT` → insert as new claim node

### Analysis

- compute propagation signals
- summarize topic growth or decline
- detect repetition, stance imbalance, and anomaly hints
- produce structured propagation summaries

### Risk

- evaluate misinformation likelihood
- flag weak evidence or suspicious propagation
- assess whether human review is required

### Report

- generate incident summaries
- create propagation analysis reports
- generate counter-message drafts

### Critic

- check unsupported claims
- check contradiction handling
- check overstatement and false certainty
- approve or reject counter-message outputs

### Visual Generator

- generate a visual clarification card via Stable Diffusion (local deployment)
- create social-media-ready counter-message images (X post format: 1200×675 px)
- text overlay rendered via Pillow to ensure English typography quality
- operate only after Critic review passes gating (hard dependency)

## 7. Multimodal Handling

Image posts should be treated as first-class evidence objects.

### Technology Choices

| Function | Tool | Rationale |
|---|---|---|
| OCR + Image Captioning | Claude Vision (`claude-sonnet-4-6`) | Single API call handles both OCR and captioning; strong English performance |
| Image Embedding | `text-embedding-3-small` (OpenAI) | Supports cross-modal text-image retrieval; cost-efficient |
| Visual Card Generation | Stable Diffusion (local deployment) | Offline operation, no third-party dependency for output assets |

### Image Post Pipeline

1. ingest image and post metadata from X API v2
2. call Claude Vision API → extract OCR text + image caption in a single request
3. extract candidate claims from merged text via LLM
4. generate image embedding via `text-embedding-3-small`
5. merge `post_text + ocr_text + image_caption`
6. index the merged evidence in Chroma (vector) and knowledge graph

### Image Understanding Outputs

- `ocr_text`: text extracted from the image
- `image_caption`: natural language description of image content
- `candidate_claims`: list of factual claims inferred from the post
- `image_type`: category label (e.g., screenshot, chart, meme, photo)
- `embedding_id`: reference to Chroma vector entry

### Visual Card Generation (Stable Diffusion)

- Triggered only after Critic review passes
- Input: structured counter-message text → prompt template → Stable Diffusion
- Output: PNG image sized for X post format (1200×675)
- Text overlay rendered via Pillow (to avoid SD font rendering issues in English)

## 8. Data Model

### Postgres

Store structured operational data:

- posts
- images
- articles
- claims
- reports
- run logs

### Chroma

Store semantic retrieval vectors for:

- post text
- OCR text
- image captions
- article chunks
- fact-check chunks

### Knowledge Graph

**Technology: Kuzu** (embedded graph database, zero-deployment, Cypher-compatible)

Rationale: runs in-process with the Python application; no separate service required; supports Cypher query syntax; suitable for MVP scale (up to tens of millions of nodes).

#### Node Types

- `post`
- `image_asset`
- `claim`
- `topic`
- `account`
- `community`
- `article`
- `fact_check`

#### Relations

- `account -> posted -> post`
- `post -> contains_claim -> claim`
- `post -> belongs_to_topic -> topic`
- `post -> uses_image -> image_asset`
- `claim -> supported_by -> article`
- `claim -> contradicted_by -> fact_check`
- `account -> belongs_to -> community`
- `image_asset -> variant_of -> image_asset`
- `claim -> same_as -> claim` (semantically equivalent, different wording)
- `claim -> related_to -> claim` (related but not equivalent)

```mermaid
flowchart TD
    AC["Account"] -->|"posted"| PO["Post"]
    PO -->|"contains_claim"| CL["Claim"]
    PO -->|"belongs_to"| TO["Topic"]
    PO -->|"uses_image"| IM["Image Asset"]
    CL -->|"supported_by"| AR["Article / Evidence"]
    CL -->|"contradicted_by"| FC["Fact Check"]
    AC -->|"belongs_to"| CO["Community"]
    IM -->|"variant_of"| IM2["Image Variant"]
```

## 9. Intent Routing

The planner should route requests into a small, controlled set of task types.

### Intent Types

- `CLAIM_ANALYSIS`
- `IMAGE_POST_ANALYSIS`
- `PROPAGATION_REPORT`
- `MISINFO_RISK_REVIEW`
- `COUNTER_MESSAGE`

### Example Routing

```mermaid
flowchart TD
    Q["User Query"] --> P["Planner"]
    P --> I1["Claim Analysis"]
    P --> I2["Image Post Analysis"]
    P --> I3["Propagation Report"]
    P --> I4["Risk Review"]
    P --> I5["Counter-Message"]

    I1 --> K1["Knowledge"]
    I1 --> A1["Analysis"]
    I1 --> R1["Risk"]

    I2 --> M2["Multimodal"]
    I2 --> K2["Knowledge"]
    I2 --> R2["Risk"]

    I3 --> K3["Knowledge"]
    I3 --> A3["Analysis"]
    I3 --> RP3["Report"]
    I3 --> C3["Critic"]

    I5 --> K5["Knowledge"]
    I5 --> G5["Message Builder"]
    I5 --> V5["Visual Generator"]
    I5 --> C5["Critic"]
```

## 10. Skills and Tool Calling

Tool calling should be implemented through **skills**, not ad hoc prompt instructions.

### OpenClaw Skill Format

Each skill is implemented as a directory containing a `SKILL.md` file. The `SKILL.md` defines the skill's purpose, input/output contract, and tool-calling instructions. Services execute the deterministic logic behind each skill.

```
skills/
  claim-retrieve/
    SKILL.md          # input/output contract, retrieval instructions
  image-post-ingest/
    SKILL.md
  propagation-analyze/
    SKILL.md
  ...
```

Workspace-level skills take precedence over global skills, allowing per-workspace overrides when needed.

### Why Skills

- stable input/output contract enforced via `SKILL.md`
- reusable across workspaces without duplication
- consistent with OpenClaw's native tool-calling model
- easier error handling and skill-level logging

### Proposed Skills

- `x-post-ingest` — fetch posts from X API v2, normalize to internal format
- `image-post-ingest` — OCR + image captioning + embedding via Claude Vision; wraps the full multimodal ingestion pipeline
- `claim-retrieve` — vector + graph retrieval of related claims, articles, fact checks
- `propagation-analyze` — compute trend metrics, stance distribution, anomaly signals
- `misinfo-risk-review` — score misinformation likelihood, flag for human review if needed
- `counter-message-build` — generate evidence-backed rebuttal text
- `counter-visual-generate` — generate clarification card via Stable Diffusion + Pillow text overlay
- `campaign-report-build` — compile full incident report
- `critic-review` — validate evidence coverage, detect overclaims, approve or reject output

### Responsibility Split

- **Planner** decides which workflow to run.
- **Skills** define how tools are called.
- **Services** execute deterministic logic.

## 11. Workflow Strategy

The workflow should be **hard-orchestrated at the top level** and **soft-agentic inside bounded stages**.

### Hard-Orchestrated

These steps should be fixed:

- intent routing
- skill sequence
- risk gate
- critic gate
- report schema
- visual generation trigger

### Soft-Agentic

These steps can be flexible:

- query rewriting
- evidence grouping
- claim normalization
- response wording
- visual prompt drafting

This keeps the system explainable and testable while retaining some agentic flexibility.

### Error and Fallback Handling

All failures must be explicitly logged and surfaced in the final report. Silent failure is not permitted.

| Failure Scenario | Handling |
|---|---|
| OCR returns empty or low confidence | Degrade: proceed with `post_text` only; flag `image_text_unavailable` in report |
| No relevant evidence retrieved | Block: set risk to `INSUFFICIENT_EVIDENCE`; route to human review; do not generate counter-message |
| Critic rejects after 2 retries | Block: send to human review queue with rejection log; halt automated output |
| Stable Diffusion generation fails | Degrade: return text counter-message only; flag `visual_card_unavailable` in report |
| Kuzu graph query timeout | Degrade: skip graph-based analysis; use vector retrieval results only; flag `graph_unavailable` |
| X API rate limit hit | Queue: defer ingestion job; retry with exponential backoff; do not drop posts |

## 12. Example End-to-End Flow

### Use Case

User asks:

> Analyze whether this image post is misleading and generate a clarification card.

### System Flow

1. `planner` classifies `IMAGE_POST_ANALYSIS + COUNTER_MESSAGE`
2. `image-post-ingest` extracts OCR text, image caption, and candidate claims via Claude Vision
3. `claim-retrieve` gathers related evidence and fact checks
4. `propagation-analyze` summarizes topic spread
5. `misinfo-risk-review` assigns a risk level
6. `counter-message-build` creates rebuttal text
7. `critic-review` checks evidence sufficiency and wording accuracy
   - Pass → continue to step 8
   - Reject → return to step 6 for revision, max 2 retries; if still rejected → route to human review queue
8. `counter-visual-generate` creates a visual clarification card via Stable Diffusion (triggered only after Critic passes)
9. `report` returns the final analyst-facing output

## 13. MVP Scope

To keep the project manageable, the first version should only do:

- **Data ingestion**: X API v2 (filtered stream or search endpoint); MVP may use a pre-collected JSON dataset of X posts to avoid API quota issues during development
- text + image post ingestion (English posts only)
- OCR + image summary via Claude Vision
- claim retrieval via Chroma + Kuzu
- propagation summary
- misinformation risk review
- counter-message text
- one visual clarification card via Stable Diffusion

Do **not** start with:

- full graph-scale community detection
- deepfake detection
- large-scale influence optimization
- real-world automatic campaign execution

## 14. Proposed Roadmap

### Phase 1: Foundation

- set up project repository and development environment
- install and configure OpenClaw runtime; initialize workspace directories for all agents
- define Postgres schema for posts, images, claims, articles, reports, run logs
- implement X API v2 ingestion pipeline (`x-post-ingest` skill + `SKILL.md`)
- implement Chroma vector store and Kuzu knowledge graph initialization
- implement planner workspace with intent classification and multi-agent routing

### Phase 2: Multimodal Evidence

- implement `image-post-ingest` skill (Claude Vision OCR + captioning + embedding)
- implement claim extraction and two-stage deduplication
- index posts and images into Chroma and Kuzu
- implement `claim-retrieve` skill

### Phase 3: Propagation Analysis and Risk

- implement `propagation-analyze` skill (trend metrics, stance distribution, anomaly signals)
- implement `misinfo-risk-review` skill with human review routing
- define graph-backed claim/topic views in Kuzu

### Phase 4: Counter-Messaging and Output

- implement `counter-message-build` skill
- implement `critic-review` skill with retry and blocking logic
- implement `counter-visual-generate` skill (Stable Diffusion + Pillow)
- implement `campaign-report-build` skill
- end-to-end integration test across all intents

## 15. Evaluation

Recommended evaluation dimensions:

| Dimension | Measurement Method | Target |
|---|---|---|
| **Retrieval relevance** | NDCG@5 on 50 annotated queries vs. BM25 baseline | > BM25 baseline |
| **Critic catch rate** | Inject 100 overclaim outputs; measure correct rejection rate | ≥ 85% |
| **Counter-message usefulness** | 3-reviewer blind scoring (1–5): factual accuracy, readability, rebuttal strength | Average ≥ 3.5 |
| Evidence coverage | % of retrieved evidence items that are directly cited in report | ≥ 70% |
| Contradiction disclosure rate | % of conflicting evidence pairs that are surfaced to analyst | ≥ 80% |
| Propagation summary quality | Human rating of trend description accuracy (1–5) | Average ≥ 3.5 |
| Misinformation risk calibration | Precision/recall on a labeled test set of 200 posts | F1 ≥ 0.75 |
| Multimodal grounding quality | % of image-derived claims traceable to OCR or caption text | ≥ 75% |

## 16. Final Recommendation

This project is built as a standalone greenfield system aligned with the course's **Society** direction:

- clean multi-agent architecture designed specifically for social media propagation analysis
- X (Twitter/X) as the primary data source via X API v2
- multimodal evidence handling (text + image) as a first-class capability
- Kuzu knowledge graph as the core relational memory for claims, accounts, and topics
- Stable Diffusion for counter-messaging visual output only, not as the primary analysis engine
- all components purpose-built for English-language, US-context social media

This architecture is technically coherent, domain-appropriate, and fully aligned with the Society direction.

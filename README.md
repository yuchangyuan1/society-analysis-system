# Society Analysis

A real-time multi-source retrieval system over social-media discussions and
authoritative news. Users ask questions in natural language; the system fans
the request out to **three retrieval branches in parallel** —

- **Evidence Retrieval** — hybrid (Dense + BM25 + RRF + Rerank) over
  authoritative news (BBC / NYT / Reuters / AP / Xinhua)
- **NL2SQL** — natural language → safe Postgres SELECT against community
  posts, with semantic topic resolution and self-correcting repair loop
- **Knowledge Graph Query** — Cypher over a Kuzu graph of accounts /
  posts / topics / entities

— then composes a citation-bearing markdown report with a Quality Critic
guarding against hallucination, and feeds Critic verdicts back into a
Reflection store that auto-curates the system's own learned exemplars.

Long-running chat sessions stay performant: the conversation list is
window-bounded and older turns are auto-compressed into a rolling
summary, so a session can run for hundreds of turns without ballooning
memory or prompt size.

> Want the design rationale? See `PROJECT_REDESIGN_V2.md`.
> Want the code map? See `workflow.md`.

---

## What this is good for

```
"What topics are trending and who's amplifying them?"
"What did BBC and Reuters say about the Iran ceasefire?"
"Is the Reddit claim 'vaccines reduce hospitalisation by 90%' supported by official sources?"
"How many angry posts about climate this week, and what entities do they mention?"
"Compare the official line on the Cuba sanctions with what users are saying."
```

The system runs three branches concurrently when the question benefits from
cross-source synthesis, then a Report Writer assembles a markdown answer
with inline citations like `[chunk_id]` that resolve to the actual BBC/NYT
URL. A Quality Critic checks every numerical claim against the SQL/KG row
it cites; if the LLM hallucinates a number, the response is flagged
`needs_human_review=True`.

---

## Quick start

### 1. Install

```bash
git clone <repo>
cd society-analysis-project-update
python -m venv .venv
.venv\Scripts\activate    # Windows
# source .venv/bin/activate   # macOS/Linux
pip install -e .
```

You also need:
- **Postgres ≥ 14** with the `pg_trgm` extension available (the schema
  installs it automatically).
- **OpenAI API key** (`OPENAI_API_KEY`) for LLM + embeddings.
- *(Optional)* **Anthropic API key** (`ANTHROPIC_API_KEY`) for the
  multimodal image-understanding agent. The pipeline degrades gracefully
  when it's missing.
- ~600 MB free for the bge-reranker-base model (downloaded on first use).

### 2. Configure

Copy `.env.example` to `.env` (or just create `.env`):

```env
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o
POSTGRES_DSN=postgresql://society:society_pass@localhost:5432/society_db

# Optional
ANTHROPIC_API_KEY=sk-ant-...
POSTGRES_READONLY_DSN=postgresql://society_ro:...@localhost:5432/society_db
MULTIMODAL_DAILY_BUDGET_USD=5.0
```

### 3. Initialize the database

```bash
# Create the database first if needed:
#   createdb society_db   (or: psql -c "CREATE DATABASE society_db;")

# Apply the v2 schema (6 tables + tsvector trigger + pg_trgm extension):
python -c "import psycopg2, config; \
  c = psycopg2.connect(config.POSTGRES_DSN); c.autocommit=True; \
  c.cursor().execute(open('db/schema_v2.sql').read())"
```

### 4. Cold-start the planner memory (Chroma 3)

```bash
python -m scripts.seed_planner_memory
```

This loads three `ModuleCard`s (one per branch) and eight workflow
exemplars so the Planner has prior art to draw on.

### 5. Run the offline ingestion (one-time, then schedule)

```bash
# Option A — quick smoke against bundled fixture (no network):
python main.py --jsonl tests/fixtures/posts_v2_smoke.jsonl

# Option B — pull live Reddit:
python main.py --subreddit conspiracy --days 3

# Pull authoritative-source articles (5 outlets):
python -m agents.official_ingestion_pipeline --once
```

After step 5 you should see:
- Postgres `posts_v2 / topics_v2 / entities_v2 / post_entities_v2` populated
- Kuzu graph at `data/kuzu_graph` with `Account → Post → Topic` edges
- Chroma collections at `data/chroma`:
  - `chroma_official` — authoritative news chunks (citation source)
  - `chroma_nl2sql` — schema descriptions + NL→SQL exemplars
  - `chroma_planner` — module cards + workflow exemplars

### 6. Start the services

```bash
# Terminal 1 — FastAPI (port 8000)
uvicorn api.app:app --reload --port 8000

# Terminal 2 — Streamlit UI (port 8501)
streamlit run ui/streamlit_app.py
```

Open <http://localhost:8501>. The Chat page is the default landing.

---

## Try it from the UI

Sample questions to see different branch combinations:

| Question | Branches expected |
|---|---|
| `What topics are trending and who's amplifying them?` | nl2sql + kg |
| `What were users discussing about misinformation?` | nl2sql + kg (semantic topic resolution) |
| `What did BBC say about the troop reduction?` | evidence |
| `Is the claim 'vaccines reduce hospitalisation by 90%' backed by official sources?` | evidence + nl2sql |
| `Compare the official line on Cuba with community sentiment.` | evidence + nl2sql + kg |
| `Top 3 most-liked posts and their authors` | nl2sql |
| `Who is most active in the most-discussed topic?` | nl2sql + kg |

The "Structured output" panel under each answer shows which branches ran,
what SQL was generated, what KG nodes were touched, and which evidence
chunks were cited. The right-side tabs (Evidence / Topic / Graph /
Metrics / Visual) populate based on which branches answered.

---

## Try it from the API

```bash
# Direct branch access (debug):
curl -X POST http://127.0.0.1:8000/retrieve/nl2sql \
  -H 'Content-Type: application/json' \
  -d '{"nl_query":"How many posts per dominant emotion?"}'

curl -X POST http://127.0.0.1:8000/retrieve/evidence \
  -H 'Content-Type: application/json' \
  -d '{"query":"US troop reduction in Germany"}'

curl -X POST http://127.0.0.1:8000/retrieve/kg \
  -H 'Content-Type: application/json' \
  -d '{"query_kind":"key_nodes","target":{"top_k":5}}'

# Full chat (all the orchestration):
curl -X POST http://127.0.0.1:8000/chat/query \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"demo","message":"What is trending and who is amplifying it?"}'

# Inspect a session:
curl http://127.0.0.1:8000/chat/session/demo

# Reflection inspector:
curl 'http://127.0.0.1:8000/reflection/chroma2?kind=success&limit=20'
curl 'http://127.0.0.1:8000/reflection/log?limit=20'
```

---

## Long-running sessions

Each chat session is stored as a single JSON file under `data/sessions/`.
The conversation list is automatically window-bounded so it stays small
and fast to load:

- Up to `SESSION_MAX_TURNS` (default **40**) turns kept verbatim.
- When that limit is exceeded, the oldest `SESSION_MIN_TURNS_TO_COMPACT`
  (default **10**) turns are compressed into a rolling `summary` field
  via one LLM call (`agents/conversation_compactor.py`).
- The Rewriter sees both the live window AND the rolling summary, so
  pronouns and topic anchors set hundreds of turns ago still resolve.
- LLM failures fall back to a plain trim, so persistence never breaks.

What you'll see in `data/sessions/<id>.json`:

```json
{
  "session_id": "demo",
  "current_topic_id": "topic_abc",
  "summary": "User asked about vaccine misinformation, ...",
  "summary_until_turn": 30,
  "archived_count": 30,
  "conversation": [/* most recent 40 turns */]
}
```

Override the window via env vars (see config table).

---

## Routine maintenance

```bash
# Daily decay sweep — drop stale experience records (kind=success / error /
# workflow_*). Anchor docs (kind=schema / module_card) are preserved.
python -m scripts.decay_chroma_experience

# Re-pull authoritative sources (BBC / NYT / Reuters / AP / Xinhua):
python -m agents.official_ingestion_pipeline --once

# Replay previously-written jsonl into Chroma 1 (after a server outage):
python -m agents.official_ingestion_pipeline --replay --date 2026-05-02

# Rebuild Chroma 2 schema part if PG ↔ Chroma drift detected:
python -m scripts.rebuild_chroma2_schema --dry-run
python -m scripts.rebuild_chroma2_schema           # applies the rebuild
```

---

## Tests

```bash
pytest tests/                                                 # 93 unit tests
PYTEST_RUN_LIVE_SCHEMA=1 pytest tests/test_schema_consistency.py
                                                              # PG ↔ Chroma 2 live check
```

---

## Configuration knobs

All variables live in `config.py` and can be overridden via `.env`. The
common ones:

| Variable | Default | Meaning |
|---|---|---|
| `OPENAI_API_KEY` | (required) | LLM + embedding access |
| `OPENAI_MODEL` | `gpt-4o` | All LLM calls (rewriter / writer / critic / NL2SQL / topic label) |
| `POSTGRES_DSN` | localhost:5432/society_db | Write connection |
| `POSTGRES_READONLY_DSN` | (falls back to DSN) | NL2SQL connection (recommend separate read-only role in prod) |
| `NL2SQL_MAX_REPAIR_ROUNDS` | 3 | Max self-correction iterations |
| `NL2SQL_RESULT_ROW_LIMIT` | 1000 | Forced LIMIT on every query |
| `NL2SQL_STATEMENT_TIMEOUT_MS` | 5000 | Postgres `statement_timeout` per query |
| `EXPERIENCE_TTL_DAYS` | 30 | Auto-decay age cutoff |
| `EXPERIENCE_MIN_CONFIDENCE` | 0.2 | Auto-decay confidence floor |
| `MULTIMODAL_DAILY_BUDGET_USD` | 5.0 | Daily image-understanding spend cap |
| `MULTIMODAL_MIN_LIKES / MIN_REPLIES` | 50 / 20 | Sample threshold for image processing |
| `SESSION_MAX_TURNS` | 40 | Per-session conversation window |
| `SESSION_MIN_TURNS_TO_COMPACT` | 10 | Minimum batch size when the compactor runs |
| `SESSION_SUMMARY_MAX_CHARS` | 1200 | Cap on the rolling summary length |

---

## Layout

```
agents/                # Pipeline + chat agents (see workflow.md §4.1)
tools/                 # Atomic operations (hybrid_retrieval / nl2sql / kg / topic_resolver)
services/              # Storage + third-party wrappers
models/                # Pydantic data contracts
api/                   # FastAPI routes (chat, retrieve, reflection, runs, artifacts)
ui/                    # Streamlit pages
db/                    # schema_v2.sql
config/                # YAML configs (official_sources.yaml)
scripts/               # CLI utilities (seed, decay, rebuild)
tests/                 # 93 unit tests + schema consistency

main.py                # Backend pipeline CLI
config.py              # Central config (env vars)
PROJECT_REDESIGN_V2.md # Design + decision log
workflow.md            # Architecture + module map (concise)
docs/phase{1..5}_done.md  # Per-phase delivery summaries
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ImportError: cannot import name 'X' from 'config'` | Stale config var removed during cleanup | Pull latest, no `.env` change needed |
| Chat answer says "I couldn't gather enough data" | Postgres / Chroma empty | Run step 5 (offline ingestion) |
| `branches_used: []` for every query | Planner / Rewriter LLM unreachable | Check `OPENAI_API_KEY`, OpenAI status |
| `needs_human_review: true` on factual answers | Critic flagged a numeric mismatch | Inspect `branch_outputs` — usually means LLM invented a number |
| Reuters / AP returning 0 chunks | Their public RSS is gone; we proxy via Google News | Already handled in `config/official_sources.yaml`; check network |
| `chroma_official: 0` after `--once` | Network blocked (CN region) | Set proxy env or use `--replay` against pre-pulled jsonl |
| Reranker download hangs | First-run model download | Wait for ~600MB; subsequent runs use the local cache |
| Schema-consistency test fails | Chroma 2 drifted from PG | `python -m scripts.rebuild_chroma2_schema` |

---

## Where the design lives

- **`PROJECT_REDESIGN_V2.md`** — full design doc with decision log (Q1-Q11)
- **`workflow.md`** — current code map, module clinic, command cheat sheet
- **`docs/phase{1..5}_done.md`** — what landed in each phase

If you're new to the codebase, read `workflow.md` first; it's the
shortest path from zero to "I know where everything is".

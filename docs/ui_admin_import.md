# UI and Manual Import Notes

This project now uses a single Streamlit page for classroom demos:

- `ui/streamlit_app.py` is the only Streamlit page.
- `ui/pages/0_Chat.py` and `ui/pages/3_Reflection.py` were removed.
- The answer view keeps the report readable, then shows route-module cards for
  `RAG`, `Knowledge Graph`, and `NL2SQL`.
- Knowledge Graph results are visualized when the KG branch returns nodes or
  edges.
- Raw branch JSON is hidden unless the user enables the technical-output toggle.

## Data Source Controls

The sidebar exposes the source scope used by chat and import operations:

- Reddit subreddits, defaulting to `worldnews`.
- Official/evidence sources: `ap`, `reuters`, `bbc`, `nyt`, `xinhua`.
- Reddit and official/evidence date ranges.
- Import mode: append new data or overwrite retained data.

The selected filters are appended to each chat turn as UI context so the
planner, NL2SQL branch, and evidence retrieval have explicit source-scope
instructions.

## Manual Import API

The UI buttons call FastAPI endpoints under `/admin/import`.

### Reddit

`POST /admin/import/reddit`

Runs the v2 precompute pipeline in a background task.

```json
{
  "subreddits": ["worldnews"],
  "start_date": "2026-05-02",
  "end_date": "2026-05-03",
  "mode": "append",
  "confirm_overwrite": false,
  "limit_per_subreddit": 100,
  "comment_limit": 100,
  "include_comments": true
}
```

`start_date` is converted to the pipeline's `reddit_days_back` parameter.

### Official / Evidence Sources

`POST /admin/import/official`

Runs `OfficialIngestionPipeline` in a background task for the selected sources.

```json
{
  "sources": ["ap", "reuters", "bbc", "nyt"],
  "start_date": "2026-05-02",
  "end_date": "2026-05-03",
  "mode": "append",
  "confirm_overwrite": false,
  "write_chroma": true
}
```

Current limitation: official import reads the configured RSS feeds' current
items. The date range is recorded and passed through UI query context, but the
crawler does not yet backfill arbitrary historical windows.

### Job Status

`GET /admin/import/jobs/{job_id}`

Returns queued/running/succeeded/failed status, request payload, result, error,
and warnings.

## Overwrite Safety

Overwrite is intentionally gated twice:

- The UI requires a visible confirmation checkbox.
- The API rejects overwrite requests unless `confirm_overwrite=true`.

Reddit overwrite clears retained PostgreSQL Reddit rows for the selected
subreddits before running the new pipeline. Official overwrite deletes matching
Chroma official chunks for the selected sources before re-importing.

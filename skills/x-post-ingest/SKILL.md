---
name: x-post-ingest
description: |
  Fetch and normalize posts from X (Twitter) API v2 or a JSONL file.
  Use when: "fetch posts", "ingest tweets", "load social media data",
  "collect X posts about topic", "load posts from file", "ingest JSONL"
version: 1.1.0
metadata: {"openclaw": {"requires": {"env": ["X_BEARER_TOKEN", "ANTHROPIC_API_KEY"]}, "primaryEnv": "X_BEARER_TOKEN"}}
allowed-tools: Bash, Read, Write
---

# Skill: x-post-ingest

## Purpose
Fetch posts from the X (Twitter/X) API v2 and normalize them to the internal `Post` format.
Also supports loading pre-collected posts from a JSONL file when API quota is exhausted.

## Workspace
`ingestion`

## Trigger Conditions
Activate this skill when the user or planner agent asks to:
- Fetch posts or tweets about a topic from X/Twitter
- Ingest social media data for analysis
- Load pre-collected posts from a JSONL file
- Collect data about a spreading narrative or hashtag

## Step-by-Step Instructions

1. **Load posts from JSONL file** (preferred for development/quota-limited):
   ```bash
   python -m services.cli ingest-jsonl --path "<jsonl_path>"
   ```

2. Write the post count and source to the handoff file:
   ```
   workspaces/shared/handoffs/ingestion_out.json
   ```

## Input Format
```json
{
  "query": "string — search query or topic",
  "max_results": "integer — max posts to fetch (default 50)",
  "jsonl_path": "string? — path to offline JSONL file (optional, overrides API call)"
}
```

## Output Format
```json
{
  "posts": [
    {
      "id": "string",
      "account_id": "string",
      "text": "string",
      "lang": "en",
      "retweet_count": "integer",
      "like_count": "integer",
      "reply_count": "integer",
      "posted_at": "ISO-8601 or null",
      "images": []
    }
  ],
  "count": "integer",
  "source": "x_api | jsonl"
}
```

## Error Handling
| Error | Action |
|---|---|
| X API rate limit | Queue ingestion; retry with exponential backoff |
| X API auth error | Log x_api_auth_error; return empty list |
| JSONL parse error | Log warning per line; skip malformed lines |
| Network timeout | Retry up to 3 times; log ingestion_timeout |

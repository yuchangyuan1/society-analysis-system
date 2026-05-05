#!/usr/bin/env bash
# Bootstrap a fresh clone end-to-end:
#   1) Verify .env exists with OPENAI_API_KEY filled in
#   2) docker compose up -d --build
#   3) Wait for the api container to become healthy
#   4) Seed Chroma 3 (planner memory) and Chroma 2 (NL2SQL emotion exemplars)
#   5) Optionally load the bundled smoke fixture (set LOAD_FIXTURE=1)
#
# Re-run is safe: every step is idempotent. Postgres schema is applied by
# Compose's docker-entrypoint-initdb.d on first boot; the seed scripts use
# stable ids or similarity-based de-duplication.
#
# Tested on Linux, macOS, and Windows (Git Bash). Requires Docker Desktop
# with Compose v2.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# ── 1. .env check ───────────────────────────────────────────────────────────
if [ ! -f .env ]; then
    echo "[bootstrap] .env missing. Creating from .env.example..."
    cp .env.example .env
    echo "[bootstrap] Edit .env and set OPENAI_API_KEY, then re-run this script."
    exit 1
fi
if ! grep -qE '^OPENAI_API_KEY=.+' .env \
   || grep -qE '^OPENAI_API_KEY=(sk-\.\.\.|)$' .env; then
    echo "[bootstrap] OPENAI_API_KEY is not set in .env. Edit it and re-run."
    exit 1
fi

# ── 2. compose up ───────────────────────────────────────────────────────────
echo "[bootstrap] docker compose up -d --build"
docker compose up -d --build

# ── 3. wait for api healthy ─────────────────────────────────────────────────
echo "[bootstrap] waiting for api to become healthy (up to 5 minutes)..."
api_cid="$(docker compose ps -q api)"
if [ -z "$api_cid" ]; then
    echo "[bootstrap] api container not found. Run 'docker compose ps' to debug."
    exit 1
fi
for i in $(seq 1 60); do
    status="$(docker inspect --format='{{.State.Health.Status}}' "$api_cid" 2>/dev/null || echo unknown)"
    if [ "$status" = "healthy" ]; then
        echo "[bootstrap] api is healthy."
        break
    fi
    if [ "$i" = "60" ]; then
        echo "[bootstrap] timeout waiting for api. Tail logs with 'docker compose logs api'."
        exit 1
    fi
    sleep 5
done

# ── 4. seed Chroma collections ──────────────────────────────────────────────
echo "[bootstrap] seeding Chroma 3 (planner memory)..."
docker compose exec -T api python -m scripts.seed_planner_memory

echo "[bootstrap] seeding Chroma 2 (NL2SQL emotion exemplars + guidance)..."
docker compose exec -T api python -m scripts.seed_emotion_nl2sql_examples

# ── 5. optional fixture load ────────────────────────────────────────────────
if [ "${LOAD_FIXTURE:-0}" = "1" ]; then
    echo "[bootstrap] loading bundled smoke fixture into Postgres + Kuzu..."
    docker compose exec -T api python main.py --jsonl tests/fixtures/posts_v2_smoke.jsonl
fi

echo ""
echo "[bootstrap] Done."
echo "  UI:  http://127.0.0.1:8501"
echo "  API: http://127.0.0.1:8000  (docs at /docs)"
echo ""
echo "  To load demo data later:  LOAD_FIXTURE=1 ./scripts/bootstrap.sh"
echo "  Or interactively:         docker compose exec api python main.py --jsonl tests/fixtures/posts_v2_smoke.jsonl"

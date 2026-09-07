# Quick start (most-run commands)

```bash
# Setup
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
playwright install chromium

# Tests. test_classify.py and test_calendar_owa.py need no browser, no network
# and no database -- this is the fast feedback loop.
python -m pytest
python -m pytest tests/test_classify.py

# Lint / format
ruff check .
ruff format --check .

# Local dev env. OP_DOCKER_IMAGE is what stops `op` blocking on the locked
# desktop app (see COMMON_MISTAKES #7). Leave it unset in the cluster.
export DATABASE_URL=postgresql://<PLACEHOLDER>
export INSITE_HOST=<PLACEHOLDER>
export COMMUTE_PAGE_PATH=<PLACEHOLDER> HOME_PAGE_PATH=<PLACEHOLDER>
export OP_ITEM_NAME=<PLACEHOLDER> OP_VAULT=<PLACEHOLDER>
export OP_SERVICE_ACCOUNT_TOKEN=ops_<PLACEHOLDER>
export OP_DOCKER_IMAGE=<PLACEHOLDER>
export DRY_RUN=true LOG_LEVEL=DEBUG

# Seed / refresh the browser profile. --manual opens a headed browser and types
# nothing for you; use it for first-run setup or when the automated path is
# blocked.
python scripts/bootstrap_session.py --headed
python scripts/bootstrap_session.py --manual
python scripts/bootstrap_session.py --headed --trace traces/bootstrap.zip

# Reconnaissance when the portal moves. Read-only, never clicks a submit.
python scripts/explore_insite.py / --shot artifacts/home.png --out artifacts/home.json
python scripts/explore_insite.py <PATH> --headed --net --wait 10

# The Microsoft 365 MCP connector (the browser-free calendar path).
# One interactive sign-in; the refresh token then renews silently.
python -m afas_declaraties.m365_mcp login
python -m afas_declaraties.m365_mcp call get_me
export CALENDAR_SOURCE=mcp      # owa (default) drives Chromium instead

# The app itself
python -m afas_declaraties.cli classify --window 9
python -m afas_declaraties.cli classify --since 2026-08-01 --until 2026-08-31
python -m afas_declaraties.cli digest --week 2026-08-31
python -m afas_declaraties.cli build --period 2026-08     # respects DRY_RUN
python -m afas_declaraties.cli slackd
python -m afas_declaraties.cli override 2026-08-31 office --actor caleb
# verdicts: office | home | absent.  `submit` is LIVE when DRY_RUN=false.

# Container. Target arch is arm64 (the cloud tier is arm64 nodes).
docker buildx bake --set '*.platform=linux/arm64,linux/amd64'
docker build -t afas-declaraties:local .

# Chart
helm lint charts/afas-declaraties
helm template afas-declaraties charts/afas-declaraties \
  -f charts/afas-declaraties/ci/default-values.yaml

# Docs (Material for MkDocs; dep: mkdocs-material)
mkdocs serve      # preview at :8000
mkdocs build      # -> ./site (gitignored)
```

**Before anything that writes to the portal:** `DRY_RUN` defaults to `true` and
`insite.create_draft()` honours it. Flipping it spends real money. Never re-run a
failed browser job blindly, retried corporate SSO logins lock the account.

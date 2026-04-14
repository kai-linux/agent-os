#!/usr/bin/env bash
# Lightweight evidence exporter: fetches GitHub stars and forks via gh api
# and writes YAML evidence files for the objectives system.
#
# Usage: bin/export_github_evidence.sh [owner/repo]
# Default repo: kai-linux/agent-os
#
# Designed to run on a cron (e.g. every 6 hours) to keep evidence fresh.

set -euo pipefail

REPO="${1:-kai-linux/agent-os}"
REPO_SLUG="${REPO//\//-}"  # e.g. kai-linux-agent-os -> kai-linux-agent-os
REPO_NAME="${REPO#*/}"     # e.g. agent-os

EVIDENCE_DIR="${EVIDENCE_DIR:-$HOME/.local/share/agent-os/evidence/${REPO_NAME}}"
mkdir -p "$EVIDENCE_DIR"

NOW=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# Fetch repo metadata via gh api (single lightweight call)
API_JSON=$(gh api "repos/${REPO}" --jq '{stars: .stargazers_count, forks: .forks_count, watchers: .subscribers_count}' 2>/dev/null) || {
  echo "Error: failed to fetch repo data for ${REPO}" >&2
  exit 1
}

STARS=$(echo "$API_JSON" | grep -o '"stars":[0-9]*' | grep -o '[0-9]*')
FORKS=$(echo "$API_JSON" | grep -o '"forks":[0-9]*' | grep -o '[0-9]*')
WATCHERS=$(echo "$API_JSON" | grep -o '"watchers":[0-9]*' | grep -o '[0-9]*')

# Write stars evidence
cat > "${EVIDENCE_DIR}/github_stars_latest.yaml" <<EOF
metric_id: github_stars
repo: "${REPO}"
observed_at: "${NOW}"
value: ${STARS}
unit: "count"
direction: "increase"
provenance: "gh api repos/${REPO} — stargazers_count"
EOF

# Write forks evidence
cat > "${EVIDENCE_DIR}/github_forks_latest.yaml" <<EOF
metric_id: github_forks
repo: "${REPO}"
observed_at: "${NOW}"
value: ${FORKS}
unit: "count"
direction: "increase"
provenance: "gh api repos/${REPO} — forks_count"
EOF

# Append to history log for trend analysis
HISTORY_FILE="${EVIDENCE_DIR}/github_metrics_history.jsonl"
echo "{\"timestamp\":\"${NOW}\",\"repo\":\"${REPO}\",\"stars\":${STARS},\"forks\":${FORKS},\"watchers\":${WATCHERS}}" >> "$HISTORY_FILE"

# Capture traffic metrics (views, clones, referrers — GitHub retains 14 days)
TRAFFIC_FILE="${EVIDENCE_DIR}/github_traffic_history.jsonl"
VIEWS_JSON=$(gh api "repos/${REPO}/traffic/views" --jq '{total_views: .count, unique_visitors: .uniques}' 2>/dev/null) || VIEWS_JSON='{"total_views":0,"unique_visitors":0}'
CLONES_JSON=$(gh api "repos/${REPO}/traffic/clones" --jq '{total_clones: .count, unique_cloners: .uniques}' 2>/dev/null) || CLONES_JSON='{"total_clones":0,"unique_cloners":0}'
REFERRERS=$(gh api "repos/${REPO}/traffic/popular/referrers" --jq '[.[] | .referrer] | join(",")' 2>/dev/null) || REFERRERS=""

TOTAL_VIEWS=$(echo "$VIEWS_JSON" | grep -o '"total_views":[0-9]*' | grep -o '[0-9]*')
UNIQUE_VISITORS=$(echo "$VIEWS_JSON" | grep -o '"unique_visitors":[0-9]*' | grep -o '[0-9]*')
TOTAL_CLONES=$(echo "$CLONES_JSON" | grep -o '"total_clones":[0-9]*' | grep -o '[0-9]*')
UNIQUE_CLONERS=$(echo "$CLONES_JSON" | grep -o '"unique_cloners":[0-9]*' | grep -o '[0-9]*')

echo "{\"timestamp\":\"${NOW}\",\"repo\":\"${REPO}\",\"views\":${TOTAL_VIEWS:-0},\"unique_visitors\":${UNIQUE_VISITORS:-0},\"clones\":${TOTAL_CLONES:-0},\"unique_cloners\":${UNIQUE_CLONERS:-0},\"referrers\":\"${REFERRERS}\"}" >> "$TRAFFIC_FILE"

echo "Evidence exported for ${REPO}: stars=${STARS} forks=${FORKS} watchers=${WATCHERS} views=${TOTAL_VIEWS:-0} clones=${TOTAL_CLONES:-0}"

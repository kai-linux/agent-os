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
REFERRERS_JSON=$(gh api "repos/${REPO}/traffic/popular/referrers" 2>/dev/null) || REFERRERS_JSON="[]"
REFERRERS=$(echo "$REFERRERS_JSON" | python3 -c "import sys,json; print(','.join(r['referrer'] for r in json.load(sys.stdin)))" 2>/dev/null) || REFERRERS=""

TOTAL_VIEWS=$(echo "$VIEWS_JSON" | grep -o '"total_views":[0-9]*' | grep -o '[0-9]*')
UNIQUE_VISITORS=$(echo "$VIEWS_JSON" | grep -o '"unique_visitors":[0-9]*' | grep -o '[0-9]*')
TOTAL_CLONES=$(echo "$CLONES_JSON" | grep -o '"total_clones":[0-9]*' | grep -o '[0-9]*')
UNIQUE_CLONERS=$(echo "$CLONES_JSON" | grep -o '"unique_cloners":[0-9]*' | grep -o '[0-9]*')

echo "{\"timestamp\":\"${NOW}\",\"repo\":\"${REPO}\",\"views\":${TOTAL_VIEWS:-0},\"unique_visitors\":${UNIQUE_VISITORS:-0},\"clones\":${TOTAL_CLONES:-0},\"unique_cloners\":${UNIQUE_CLONERS:-0},\"referrers\":\"${REFERRERS}\"}" >> "$TRAFFIC_FILE"

# Capture daily views/clones breakdown and referrer details for funnel analysis
DAILY_FILE="${EVIDENCE_DIR}/github_daily_traffic.jsonl"
VIEWS_DAILY=$(gh api "repos/${REPO}/traffic/views" 2>/dev/null) || VIEWS_DAILY='{"count":0,"uniques":0,"views":[]}'
CLONES_DAILY=$(gh api "repos/${REPO}/traffic/clones" 2>/dev/null) || CLONES_DAILY='{"count":0,"uniques":0,"clones":[]}'
PATHS_JSON=$(gh api "repos/${REPO}/traffic/popular/paths" 2>/dev/null) || PATHS_JSON="[]"

python3 -c "
import json, sys
now = '${NOW}'
repo = '${REPO}'
views = json.loads('''${VIEWS_DAILY}''')
clones = json.loads('''${CLONES_DAILY}''')
referrers = json.loads('''${REFERRERS_JSON}''')
paths = json.loads('''${PATHS_JSON}''')
record = {
    'snapshot_at': now,
    'repo': repo,
    'views_14d': {'total': views.get('count',0), 'uniques': views.get('uniques',0)},
    'clones_14d': {'total': clones.get('count',0), 'uniques': clones.get('uniques',0)},
    'daily_views': [{'date': v['timestamp'][:10], 'count': v['count'], 'uniques': v['uniques']} for v in views.get('views',[])],
    'daily_clones': [{'date': c['timestamp'][:10], 'count': c['count'], 'uniques': c['uniques']} for c in clones.get('clones',[])],
    'referrers': [{'source': r['referrer'], 'count': r['count'], 'uniques': r['uniques']} for r in referrers],
    'popular_paths': [{'path': p['path'], 'title': p['title'], 'count': p['count'], 'uniques': p['uniques']} for p in paths[:10]],
}
print(json.dumps(record))
" >> "$DAILY_FILE" 2>/dev/null || true

echo "Evidence exported for ${REPO}: stars=${STARS} forks=${FORKS} watchers=${WATCHERS} views=${TOTAL_VIEWS:-0} clones=${TOTAL_CLONES:-0}"

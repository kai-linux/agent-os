#!/usr/bin/env bash
# Publish the Agent OS case study to external platforms and capture adoption metrics.
#
# Supported platforms:
#   dev.to  — requires DEV_API_KEY env var
#
# Usage: bin/publish_case_study.sh [--dry-run]
#
# After publishing, run bin/export_github_evidence.sh to capture adoption baseline.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

DEVTO_ARTICLE="${REPO_ROOT}/docs/promotion/devto-article.md"
METRICS_DIR="${REPO_ROOT}/runtime/metrics"
DISTRIBUTION_LOG="${METRICS_DIR}/distribution_log.jsonl"
mkdir -p "$METRICS_DIR"

NOW=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

log_distribution() {
  local platform="$1" status="$2" detail="$3"
  echo "{\"timestamp\":\"${NOW}\",\"platform\":\"${platform}\",\"status\":\"${status}\",\"detail\":\"${detail}\"}" >> "$DISTRIBUTION_LOG"
}

# --- dev.to ---
publish_devto() {
  if [[ -z "${DEV_API_KEY:-}" ]]; then
    echo "SKIP: DEV_API_KEY not set — cannot publish to dev.to"
    log_distribution "devto" "skipped" "DEV_API_KEY not set"
    return 1
  fi

  if [[ ! -f "$DEVTO_ARTICLE" ]]; then
    echo "ERROR: dev.to article not found at $DEVTO_ARTICLE"
    log_distribution "devto" "error" "article file missing"
    return 1
  fi

  # Read article content
  ARTICLE_BODY=$(cat "$DEVTO_ARTICLE")

  if $DRY_RUN; then
    echo "DRY RUN: Would publish to dev.to (${#ARTICLE_BODY} chars)"
    log_distribution "devto" "dry_run" "${#ARTICLE_BODY} chars"
    return 0
  fi

  # Publish via dev.to API
  RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "https://dev.to/api/articles" \
    -H "api-key: ${DEV_API_KEY}" \
    -H "Content-Type: application/json" \
    -d "$(python3 -c "
import json, sys
body = open('${DEVTO_ARTICLE}').read()
print(json.dumps({'article': {'body_markdown': body}}))" 2>/dev/null)" 2>/dev/null) || {
    echo "ERROR: dev.to API request failed"
    log_distribution "devto" "error" "API request failed"
    return 1
  }

  HTTP_CODE=$(echo "$RESPONSE" | tail -1)
  BODY=$(echo "$RESPONSE" | sed '$d')

  if [[ "$HTTP_CODE" == "201" ]]; then
    URL=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('url',''))" 2>/dev/null || echo "")
    echo "SUCCESS: Published to dev.to — $URL"
    log_distribution "devto" "published" "$URL"
  else
    echo "ERROR: dev.to returned HTTP $HTTP_CODE"
    log_distribution "devto" "error" "HTTP $HTTP_CODE"
    return 1
  fi
}

# --- Metrics snapshot ---
capture_metrics() {
  echo "Capturing adoption metrics baseline..."
  if [[ -x "${REPO_ROOT}/bin/export_github_evidence.sh" ]]; then
    "${REPO_ROOT}/bin/export_github_evidence.sh" kai-linux/agent-os 2>/dev/null || true
  fi

  # Capture additional metrics via gh API
  REPO_DATA=$(gh api repos/kai-linux/agent-os --jq '{stars: .stargazers_count, forks: .forks_count, watchers: .subscribers_count, open_issues: .open_issues_count}' 2>/dev/null) || {
    echo "WARN: Could not fetch GitHub metrics"
    return 0
  }

  STARS=$(echo "$REPO_DATA" | python3 -c "import sys,json; print(json.load(sys.stdin)['stars'])" 2>/dev/null || echo "0")
  FORKS=$(echo "$REPO_DATA" | python3 -c "import sys,json; print(json.load(sys.stdin)['forks'])" 2>/dev/null || echo "0")

  echo "{\"timestamp\":\"${NOW}\",\"event\":\"case_study_distribution\",\"stars\":${STARS},\"forks\":${FORKS}}" >> "$DISTRIBUTION_LOG"
  echo "Baseline: stars=${STARS} forks=${FORKS}"
}

# --- Main ---
echo "=== Agent OS Case Study Distribution ==="
echo "Date: ${NOW}"
echo ""

publish_devto || true
echo ""
capture_metrics

echo ""
echo "Distribution log: ${DISTRIBUTION_LOG}"
echo ""
echo "Manual steps required:"
echo "  - Hacker News: Submit at https://news.ycombinator.com/submit"
echo "    Title and comment text in: docs/promotion/hn-submission.md"

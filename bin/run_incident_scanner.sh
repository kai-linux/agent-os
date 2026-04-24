#!/usr/bin/env bash
# Incident scanner — every 6 hours reads the last 24h of incident signals
# (runtime/incidents/incidents.jsonl, runtime/mailbox/escalated/*.md,
# runtime/audit/audit.jsonl for anomaly events) and files self-fix GitHub
# issues in agent-os for recurring patterns. Dispatcher/groomer picks the
# issues up like any other work, closing the loop between "something broke
# at runtime" and "agent fixes the class of bug."
#
# Suggested crontab entry (every 6h at :15):
#   15 */6 * * * /path/to/agent-os/bin/run_incident_scanner.sh >> /path/to/agent-os/runtime/logs/incident_scanner.log 2>&1
set -euo pipefail

# shellcheck source=bin/common_env.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common_env.sh"

log_cron_start "incident_scanner"

cd "$ROOT"
"$ROOT/.venv/bin/python3" -m orchestrator.incident_scanner

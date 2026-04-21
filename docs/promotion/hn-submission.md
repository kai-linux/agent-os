# Hacker News Submission

## Title
Show HN: Agent OS – supervised rollout for issue-to-PR automation with public reliability metrics

## URL
https://github.com/kai-linux/agent-os

## Text (for Show HN comment)

Agent OS is a coordination layer for AI agents that turns GitHub issues into reviewable PRs with CI gating, retries, bounded escalation, and a public reliability dashboard.

Current live metrics are published in the repo. As of 2026-04-21, the rolling 14-day dashboard shows:
- 69% task success rate (61/88)
- 11% escalation rate (10/88)
- mean completion time of 0.1h

The important framing is rollout, not hype:
- start a new repo in `dispatcher_only` mode
- give it 5 to 10 bounded issues with good tests
- review the first PRs manually
- turn on the full planner/groomer loop only after the pilot is stable

The recursive self-improvement loop is still the interesting part: the system analyzes failures, files fix tickets, and routes those tickets back through the same delivery path.

Also honest about failures: the PR-98 cascade is documented in the case study, and the repo now explicitly recommends supervised rollout for external repos instead of pretending every workload is ready for full autonomy on day one.

Case study: https://github.com/kai-linux/agent-os/blob/main/docs/case-study-agent-os.md
Discussion: https://github.com/kai-linux/agent-os/discussions/167

Try it: `git clone && gh auth login && ./demo.sh`

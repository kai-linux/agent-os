# Reliability Dashboard

Updated: 2026-04-15T03:00:02.282735+00:00
Window: rolling 14 days
Sources: `runtime/metrics/agent_stats.jsonl` + `PRODUCTION_FEEDBACK.md`

| Metric | Value |
|---|---|
| Task success rate | 92% (46/50) |
| Mean completion time | 0.1h |
| Escalation rate | 0% (0/50) |
| GitHub stars | 2 |
| GitHub forks | 0 |

## 14-Day Momentum

| Period | Success | Mean time | Escalation |
|---|---|---|---|
| Last 7 days | 93% (27/29) | 0.1h | 0% |
| Prior 7 days | 90% (19/21) | 0.1h | 0% |

## Daily Trend

| Date | Success | Escalation | Volume |
|---|---|---|---|
| 2026-04-02 | `............` n/a | `............` n/a | 0 |
| 2026-04-03 | `............` n/a | `............` n/a | 0 |
| 2026-04-04 | `############` 100% | `............` 0% | 4 |
| 2026-04-05 | `############` 100% | `............` 0% | 4 |
| 2026-04-06 | `############` 100% | `............` 0% | 3 |
| 2026-04-07 | `############` 100% | `............` 0% | 4 |
| 2026-04-08 | `############` 100% | `............` 0% | 5 |
| 2026-04-09 | `##########..` 83% | `............` 0% | 12 |
| 2026-04-10 | `############` 100% | `............` 0% | 2 |
| 2026-04-11 | `............` n/a | `............` n/a | 0 |
| 2026-04-12 | `############` 100% | `............` 0% | 5 |
| 2026-04-13 | `............` n/a | `............` n/a | 0 |
| 2026-04-14 | `############` 100% | `............` 0% | 5 |
| 2026-04-15 | `............` n/a | `............` n/a | 0 |

## Per-Agent Breakdown

| Agent | Success | Volume |
|---|---|---|
| deepseek | 100% (3/3) | 3 |
| claude | 95% (37/39) | 39 |
| codex | 75% (6/8) | 8 |

## Top Blocker Categories

- `missing_credentials`: 3
- `missing_context`: 2
- `quota_limited`: 2
- `dependency_blocked`: 1

## Notes

- Public-safe aggregates only: no task bodies, escalation notes, or operator-sensitive logs.
- This page prefers live `agent_stats.jsonl` aggregates and falls back to `PRODUCTION_FEEDBACK.md` when needed.

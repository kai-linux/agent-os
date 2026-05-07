# Reliability Dashboard

Updated: 2026-05-07T03:00:02.248826+00:00
Window: rolling 14 days
Sources: `runtime/metrics/agent_stats.jsonl` + `PRODUCTION_FEEDBACK.md`

| Metric | Value |
|---|---|
| Task success rate | 98% (49/50) |
| Mean completion time | 0.1h |
| Escalation rate | 2% (1/50) |
| GitHub stars | 3 |
| GitHub forks | 0 |

## 14-Day Momentum

| Period | Success | Mean time | Escalation |
|---|---|---|---|
| Last 7 days | n/a (0/0) | n/a | n/a |
| Prior 7 days | 98% (49/50) | 0.1h | 2% |

## Daily Trend

| Date | Success | Escalation | Volume |
|---|---|---|---|
| 2026-04-24 | `############` 100% | `............` 0% | 18 |
| 2026-04-25 | `............` n/a | `............` n/a | 0 |
| 2026-04-26 | `............` n/a | `............` n/a | 0 |
| 2026-04-27 | `............` n/a | `............` n/a | 0 |
| 2026-04-28 | `............` n/a | `............` n/a | 0 |
| 2026-04-29 | `............` n/a | `............` n/a | 0 |
| 2026-04-30 | `............` n/a | `............` n/a | 0 |
| 2026-05-01 | `............` n/a | `............` n/a | 0 |
| 2026-05-02 | `............` n/a | `............` n/a | 0 |
| 2026-05-03 | `............` n/a | `............` n/a | 0 |
| 2026-05-04 | `............` n/a | `............` n/a | 0 |
| 2026-05-05 | `............` n/a | `............` n/a | 0 |
| 2026-05-06 | `............` n/a | `............` n/a | 0 |
| 2026-05-07 | `............` n/a | `............` n/a | 0 |

## Per-Agent Breakdown

| Agent | Success | Volume |
|---|---|---|
| codex | 100% (42/42) | 42 |
| claude | 88% (7/8) | 8 |

## Top Blocker Categories

- `invalid_result_contract`: 2

## Notes

- Public-safe aggregates only: no task bodies, escalation notes, or operator-sensitive logs.
- This page prefers live `agent_stats.jsonl` aggregates and falls back to `PRODUCTION_FEEDBACK.md` when needed.

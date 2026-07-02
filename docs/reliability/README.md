# Reliability Dashboard

Updated: 2026-07-02T03:00:02.395103+00:00
Window: rolling 14 days
Sources: `runtime/metrics/agent_stats.jsonl` + `PRODUCTION_FEEDBACK.md`

| Metric | Value |
|---|---|
| Task success rate | 75% (117/155) |
| Mean completion time | 0.1h |
| Escalation rate | 14% (22/155) |
| GitHub stars | 4 |
| GitHub forks | 0 |

## 14-Day Momentum

| Period | Success | Mean time | Escalation |
|---|---|---|---|
| Last 7 days | n/a (0/0) | n/a | n/a |
| Prior 7 days | n/a (0/0) | n/a | n/a |

## Daily Trend

| Date | Success | Escalation | Volume |
|---|---|---|---|
| 2026-06-19 | `............` n/a | `............` n/a | 0 |
| 2026-06-20 | `............` n/a | `............` n/a | 0 |
| 2026-06-21 | `............` n/a | `............` n/a | 0 |
| 2026-06-22 | `............` n/a | `............` n/a | 0 |
| 2026-06-23 | `............` n/a | `............` n/a | 0 |
| 2026-06-24 | `............` n/a | `............` n/a | 0 |
| 2026-06-25 | `............` n/a | `............` n/a | 0 |
| 2026-06-26 | `............` n/a | `............` n/a | 0 |
| 2026-06-27 | `............` n/a | `............` n/a | 0 |
| 2026-06-28 | `............` n/a | `............` n/a | 0 |
| 2026-06-29 | `............` n/a | `............` n/a | 0 |
| 2026-06-30 | `............` n/a | `............` n/a | 0 |
| 2026-07-01 | `............` n/a | `............` n/a | 0 |
| 2026-07-02 | `............` n/a | `............` n/a | 0 |

## Per-Agent Breakdown

| Agent | Success | Volume |
|---|---|---|
| claude | 77% (50/65) | 65 |
| codex | 79% (64/81) | 81 |
| deepseek | 40% (2/5) | 5 |
| gemini | 25% (1/4) | 4 |

## Top Blocker Categories

- `no_diff_produced`: 8
- `dependency_blocked`: 7
- `invalid_result_contract`: 7
- `prompt_too_large`: 6
- `missing_credentials`: 5

## Notes

- Public-safe aggregates only: no task bodies, escalation notes, or operator-sensitive logs.
- This page prefers live `agent_stats.jsonl` aggregates and falls back to `PRODUCTION_FEEDBACK.md` when needed.

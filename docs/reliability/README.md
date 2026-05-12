# Reliability Dashboard

Updated: 2026-05-12T03:00:01.721216+00:00
Window: rolling 14 days
Sources: `runtime/metrics/agent_stats.jsonl` + `PRODUCTION_FEEDBACK.md`

| Metric | Value |
|---|---|
| Task success rate | 75% (117/155) |
| Mean completion time | 0.1h |
| Escalation rate | 14% (22/155) |
| GitHub stars | 3 |
| GitHub forks | 0 |

## 14-Day Momentum

| Period | Success | Mean time | Escalation |
|---|---|---|---|
| Last 7 days | n/a (0/0) | n/a | n/a |
| Prior 7 days | n/a (0/0) | n/a | n/a |

## Daily Trend

| Date | Success | Escalation | Volume |
|---|---|---|---|
| 2026-04-29 | `............` n/a | `............` n/a | 0 |
| 2026-04-30 | `............` n/a | `............` n/a | 0 |
| 2026-05-01 | `............` n/a | `............` n/a | 0 |
| 2026-05-02 | `............` n/a | `............` n/a | 0 |
| 2026-05-03 | `............` n/a | `............` n/a | 0 |
| 2026-05-04 | `............` n/a | `............` n/a | 0 |
| 2026-05-05 | `............` n/a | `............` n/a | 0 |
| 2026-05-06 | `............` n/a | `............` n/a | 0 |
| 2026-05-07 | `............` n/a | `............` n/a | 0 |
| 2026-05-08 | `............` n/a | `............` n/a | 0 |
| 2026-05-09 | `............` n/a | `............` n/a | 0 |
| 2026-05-10 | `............` n/a | `............` n/a | 0 |
| 2026-05-11 | `............` n/a | `............` n/a | 0 |
| 2026-05-12 | `............` n/a | `............` n/a | 0 |

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

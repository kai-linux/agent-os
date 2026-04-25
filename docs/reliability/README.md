# Reliability Dashboard

Updated: 2026-04-25T03:00:02.288900+00:00
Window: rolling 14 days
Sources: `runtime/metrics/agent_stats.jsonl` + `PRODUCTION_FEEDBACK.md`

| Metric | Value |
|---|---|
| Task success rate | 76% (122/160) |
| Mean completion time | 0.1h |
| Escalation rate | 14% (22/160) |
| GitHub stars | 3 |
| GitHub forks | 0 |

## 14-Day Momentum

| Period | Success | Mean time | Escalation |
|---|---|---|---|
| Last 7 days | 79% (101/128) | 0.1h | 11% |
| Prior 7 days | 66% (21/32) | 0.1h | 25% |

## Daily Trend

| Date | Success | Escalation | Volume |
|---|---|---|---|
| 2026-04-12 | `############` 100% | `............` 0% | 5 |
| 2026-04-13 | `............` n/a | `............` n/a | 0 |
| 2026-04-14 | `############` 100% | `............` 0% | 5 |
| 2026-04-15 | `############` 100% | `............` 0% | 5 |
| 2026-04-16 | `............` n/a | `............` n/a | 0 |
| 2026-04-17 | `####........` 35% | `######......` 47% | 17 |
| 2026-04-18 | `######......` 50% | `#...........` 7% | 28 |
| 2026-04-19 | `############` 100% | `............` 0% | 5 |
| 2026-04-20 | `............` n/a | `............` n/a | 0 |
| 2026-04-21 | `##########..` 80% | `##..........` 17% | 35 |
| 2026-04-22 | `######......` 50% | `######......` 50% | 10 |
| 2026-04-23 | `############` 97% | `............` 3% | 32 |
| 2026-04-24 | `############` 100% | `............` 0% | 18 |
| 2026-04-25 | `............` n/a | `............` n/a | 0 |

## Per-Agent Breakdown

| Agent | Success | Volume |
|---|---|---|
| codex | 79% (64/81) | 81 |
| claude | 79% (55/70) | 70 |
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

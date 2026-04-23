# Reliability Dashboard

Updated: 2026-04-23T03:00:01.800623+00:00
Window: rolling 14 days
Sources: `runtime/metrics/agent_stats.jsonl` + `PRODUCTION_FEEDBACK.md`

| Metric | Value |
|---|---|
| Task success rate | 69% (85/124) |
| Mean completion time | 0.1h |
| Escalation rate | 17% (21/124) |
| GitHub stars | 2 |
| GitHub forks | 0 |

## 14-Day Momentum

| Period | Success | Mean time | Escalation |
|---|---|---|---|
| Last 7 days | 61% (58/95) | 0.1h | 22% |
| Prior 7 days | 93% (27/29) | 0.1h | 0% |

## Daily Trend

| Date | Success | Escalation | Volume |
|---|---|---|---|
| 2026-04-10 | `############` 100% | `............` 0% | 2 |
| 2026-04-11 | `............` n/a | `............` n/a | 0 |
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
| 2026-04-23 | `............` n/a | `............` n/a | 0 |

## Per-Agent Breakdown

| Agent | Success | Volume |
|---|---|---|
| claude | 77% (55/71) | 71 |
| deepseek | 62% (5/8) | 8 |
| codex | 59% (24/41) | 41 |
| gemini | 25% (1/4) | 4 |

## Top Blocker Categories

- `dependency_blocked`: 8
- `missing_credentials`: 8
- `no_diff_produced`: 8
- `quota_limited`: 7
- `prompt_too_large`: 6

## Notes

- Public-safe aggregates only: no task bodies, escalation notes, or operator-sensitive logs.
- This page prefers live `agent_stats.jsonl` aggregates and falls back to `PRODUCTION_FEEDBACK.md` when needed.

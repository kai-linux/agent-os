# Reliability Dashboard

Updated: 2026-04-21T03:00:01.580955+00:00
Window: rolling 14 days
Sources: `runtime/metrics/agent_stats.jsonl` + `PRODUCTION_FEEDBACK.md`

| Metric | Value |
|---|---|
| Task success rate | 69% (61/88) |
| Mean completion time | 0.1h |
| Escalation rate | 11% (10/88) |
| GitHub stars | 2 |
| GitHub forks | 0 |

## 14-Day Momentum

| Period | Success | Mean time | Escalation |
|---|---|---|---|
| Last 7 days | 58% (35/60) | 0.1h | 17% |
| Prior 7 days | 93% (26/28) | 0.1h | 0% |

## Daily Trend

| Date | Success | Escalation | Volume |
|---|---|---|---|
| 2026-04-08 | `############` 100% | `............` 0% | 5 |
| 2026-04-09 | `##########..` 83% | `............` 0% | 12 |
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
| 2026-04-21 | `............` n/a | `............` n/a | 0 |

## Per-Agent Breakdown

| Agent | Success | Volume |
|---|---|---|
| claude | 77% (53/69) | 69 |
| deepseek | 62% (5/8) | 8 |
| codex | 29% (2/7) | 7 |
| gemini | 25% (1/4) | 4 |

## Top Blocker Categories

- `dependency_blocked`: 8
- `missing_credentials`: 8
- `quota_limited`: 7
- `no_diff_produced`: 7
- `runner_failure`: 3

## Notes

- Public-safe aggregates only: no task bodies, escalation notes, or operator-sensitive logs.
- This page prefers live `agent_stats.jsonl` aggregates and falls back to `PRODUCTION_FEEDBACK.md` when needed.

# Reliability Dashboard

Updated: 2026-04-18T03:00:01.741806+00:00
Window: rolling 14 days
Sources: `runtime/metrics/agent_stats.jsonl` + `PRODUCTION_FEEDBACK.md`

| Metric | Value |
|---|---|
| Task success rate | 80% (53/66) |
| Mean completion time | 0.1h |
| Escalation rate | 33% (32/97) |
| GitHub stars | 2 |
| GitHub forks | 0 |

## 14-Day Momentum

| Period | Success | Mean time | Escalation |
|---|---|---|---|
| Last 7 days | 66% (21/32) | 0.1h | 51% |
| Prior 7 days | 94% (32/34) | 0.1h | 0% |

## Daily Trend

| Date | Success | Escalation | Volume |
|---|---|---|---|
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
| 2026-04-15 | `############` 100% | `............` 0% | 5 |
| 2026-04-16 | `............` n/a | `............` n/a | 0 |
| 2026-04-17 | `###.........` 27% | `########....` 67% | 48 |
| 2026-04-18 | `............` n/a | `............` n/a | 0 |

## Per-Agent Breakdown

| Agent | Success | Volume |
|---|---|---|
| claude | 92% (45/49) | 49 |
| deepseek | 62% (5/8) | 8 |
| codex | 40% (2/5) | 5 |
| gemini | 25% (1/4) | 4 |

## Top Blocker Categories

- `fallback_exhausted`: 31
- `missing_credentials`: 8
- `quota_limited`: 5
- `dependency_blocked`: 4
- `runner_failure`: 3

## Notes

- Public-safe aggregates only: no task bodies, escalation notes, or operator-sensitive logs.
- This page prefers live `agent_stats.jsonl` aggregates and falls back to `PRODUCTION_FEEDBACK.md` when needed.

# Reliability Dashboard

Updated: 2026-05-05T03:00:02.457682+00:00
Window: rolling 14 days
Sources: `runtime/metrics/agent_stats.jsonl` + `PRODUCTION_FEEDBACK.md`

| Metric | Value |
|---|---|
| Task success rate | 86% (82/95) |
| Mean completion time | 0.1h |
| Escalation rate | 13% (12/95) |
| GitHub stars | 3 |
| GitHub forks | 0 |

## 14-Day Momentum

| Period | Success | Mean time | Escalation |
|---|---|---|---|
| Last 7 days | n/a (0/0) | n/a | n/a |
| Prior 7 days | 86% (82/95) | 0.1h | 13% |

## Daily Trend

| Date | Success | Escalation | Volume |
|---|---|---|---|
| 2026-04-22 | `######......` 50% | `######......` 50% | 10 |
| 2026-04-23 | `############` 97% | `............` 3% | 32 |
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

## Per-Agent Breakdown

| Agent | Success | Volume |
|---|---|---|
| claude | 95% (18/19) | 19 |
| codex | 84% (64/76) | 76 |

## Top Blocker Categories

- `invalid_result_contract`: 7
- `prompt_too_large`: 6
- `no_diff_produced`: 1

## Notes

- Public-safe aggregates only: no task bodies, escalation notes, or operator-sensitive logs.
- This page prefers live `agent_stats.jsonl` aggregates and falls back to `PRODUCTION_FEEDBACK.md` when needed.

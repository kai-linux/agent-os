# Reliability Dashboard

Updated: 2026-05-02T03:00:01.835362+00:00
Window: rolling 14 days
Sources: `runtime/metrics/agent_stats.jsonl` + `PRODUCTION_FEEDBACK.md`

| Metric | Value |
|---|---|
| Task success rate | 79% (101/128) |
| Mean completion time | 0.1h |
| Escalation rate | 11% (14/128) |
| GitHub stars | 3 |
| GitHub forks | 0 |

## 14-Day Momentum

| Period | Success | Mean time | Escalation |
|---|---|---|---|
| Last 7 days | n/a (0/0) | n/a | n/a |
| Prior 7 days | 79% (101/128) | 0.1h | 11% |

## Daily Trend

| Date | Success | Escalation | Volume |
|---|---|---|---|
| 2026-04-19 | `############` 100% | `............` 0% | 5 |
| 2026-04-20 | `............` n/a | `............` n/a | 0 |
| 2026-04-21 | `##########..` 80% | `##..........` 17% | 35 |
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

## Per-Agent Breakdown

| Agent | Success | Volume |
|---|---|---|
| codex | 82% (64/78) | 78 |
| claude | 74% (37/50) | 50 |

## Top Blocker Categories

- `no_diff_produced`: 8
- `invalid_result_contract`: 7
- `prompt_too_large`: 6
- `dependency_blocked`: 4
- `quota_limited`: 2

## Notes

- Public-safe aggregates only: no task bodies, escalation notes, or operator-sensitive logs.
- This page prefers live `agent_stats.jsonl` aggregates and falls back to `PRODUCTION_FEEDBACK.md` when needed.

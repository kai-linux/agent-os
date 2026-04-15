# Adoption Metrics Tracking — Case Study Distribution

> Tracks adoption impact from multi-agent case study distribution across platforms.
> Baseline established 2026-04-15. Measurement windows: 7-day and 14-day post-publication.

## Distribution Status

| Platform | Status | Date | URL |
|---|---|---|---|
| GitHub Discussions | Published | 2026-04-09 | [#167](https://github.com/kai-linux/agent-os/discussions/167) |
| dev.to | Pending | — | Requires `DEV_API_KEY` env var |
| Hacker News | Pending | — | Manual: [hn-submission.md](promotion/hn-submission.md) |
| Reddit r/programming | Pending | — | Manual: [reddit-posts.md](promotion/reddit-posts.md) |
| Reddit r/SideProject | Pending | — | Manual: [reddit-posts.md](promotion/reddit-posts.md) |
| Reddit r/LocalLLaMA | Pending | — | Manual: [reddit-posts.md](promotion/reddit-posts.md) |
| Reddit r/selfhosted | Pending | — | Manual: [reddit-posts.md](promotion/reddit-posts.md) |

## Baseline Metrics (2026-04-15)

| Metric | Value | Source |
|---|---|---|
| GitHub stars | 2 | `gh api repos/kai-linux/agent-os` |
| GitHub forks | 0 | `gh api repos/kai-linux/agent-os` |
| Unique visitors (14d) | 4 | `gh api repos/kai-linux/agent-os/traffic/views` |
| Total views (14d) | 60 | `gh api repos/kai-linux/agent-os/traffic/views` |
| Referrers | github.com only (3 uniques) | `gh api repos/kai-linux/agent-os/traffic/popular/referrers` |
| Discussion #167 upvotes | 1 | Manual check |
| Discussion #167 comments | 0 | Manual check |

## Measurement Framework

### Metrics Tracked Per Platform

For each distribution event, capture at T+0 (publication), T+7d, and T+14d:

| Metric ID | Description | Source | Direction |
|---|---|---|---|
| `github_stars` | Total star count | `gh api repos/{slug}` | increase |
| `github_stars_14d` | Star delta over 14d window | Computed from history | increase |
| `github_forks` | Total fork count | `gh api repos/{slug}` | increase |
| `github_forks_14d` | Fork delta over 14d window | Computed from history | increase |
| `unique_visitors` | Unique visitors (14d rolling) | `gh api .../traffic/views` | increase |
| `referrer_traffic` | Referrer breakdown | `gh api .../traffic/popular/referrers` | new sources |
| `discussion_engagement` | Upvotes + comments on #167 | Manual check | increase |

### Attribution Method

1. **Temporal correlation**: Star/fork/traffic spikes within 48h of a distribution event are attributed to that platform
2. **Referrer matching**: GitHub traffic referrers (dev.to, news.ycombinator.com, reddit.com) directly attribute to source
3. **Baseline subtraction**: Only deltas above baseline trend are attributed to distribution activity

### Data Collection

Automated via `bin/export_github_evidence.sh` (runs daily via cron at 05:00 UTC).
Manual snapshots captured via:

```bash
gh api repos/kai-linux/agent-os --jq '{stars: .stargazers_count, forks: .forks_count}'
gh api repos/kai-linux/agent-os/traffic/views
gh api repos/kai-linux/agent-os/traffic/popular/referrers
```

Distribution events logged to `runtime/metrics/distribution_log.jsonl`.

## Expected Platform ROI (Pre-Distribution Estimates)

| Platform | Expected Reach | Signal Quality | Effort | Expected ROI |
|---|---|---|---|---|
| Hacker News | High (Show HN) | High — technical audience matches target | Low (submit + comment) | High |
| dev.to | Medium | Medium — broad dev audience | Low (API publish) | Medium |
| r/programming | Medium-High | Medium — large but noisy | Low (post) | Medium |
| r/SideProject | Low-Medium | High — exact target audience | Low (post) | Medium |
| r/LocalLLaMA | Medium | High — multi-agent interest | Low (post) | Medium-High |
| r/selfhosted | Low-Medium | Medium — infrastructure interest | Low (post) | Low-Medium |
| GitHub Discussions | Low (internal) | N/A — existing users only | Done | Low |

## ROI Analysis Template (Post-Distribution)

Fill in after T+7d and T+14d measurements:

### T+7d Snapshot (target date: ______)

| Metric | Baseline | T+7d | Delta | Attribution |
|---|---|---|---|---|
| Stars | 2 | | | |
| Forks | 0 | | | |
| Unique visitors | 4 | | | |
| New referrers | 0 | | | |

### T+14d Snapshot (target date: ______)

| Metric | Baseline | T+14d | Delta | Attribution |
|---|---|---|---|---|
| Stars | 2 | | | |
| Forks | 0 | | | |
| Unique visitors | 4 | | | |
| New referrers | 0 | | | |

### Platform Performance Ranking

| Rank | Platform | Stars Attributed | Traffic Attributed | Recommendation |
|---|---|---|---|---|
| 1 | | | | |
| 2 | | | | |
| 3 | | | | |

### Recommendations for Future Content Strategy

_(To be filled after T+14d measurement)_

1. **Top-performing channel**: ______
2. **Scale recommendation**: ______
3. **Content format that worked**: ______
4. **Content format that didn't**: ______
5. **Next content piece priority**: ______

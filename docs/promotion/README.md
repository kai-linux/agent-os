# Promotion Content — Agent OS Case Study

Publish-ready drafts for cross-posting the Agent OS multi-agent case study.

## Files

| File | Platform | Status |
|---|---|---|
| [devto-article.md](devto-article.md) | dev.to | Ready to publish (requires `DEV_API_KEY`) |
| [hn-submission.md](hn-submission.md) | Hacker News | Ready to submit (manual) |
| [reddit-posts.md](reddit-posts.md) | Reddit (4 subreddits) | Ready to post (manual) |

## Target Platforms

| Platform | Type | Audience Match | Post Ready |
|---|---|---|---|
| dev.to | Article | Broad dev | Yes |
| Hacker News (Show HN) | Link + comment | Technical builders | Yes |
| r/programming | Post | General dev | Yes |
| r/SideProject | Post | Solo builders | Yes |
| r/LocalLLaMA | Post | Multi-agent/LLM | Yes |
| r/selfhosted | Post | Self-hosted infra | Yes |
| GitHub Discussions #167 | Discussion | Existing visitors | Published 2026-04-09 |

## Publishing Instructions

### dev.to (automated)

Set `DEV_API_KEY` environment variable, then run:
```bash
bin/publish_case_study.sh
```

### Hacker News (manual)
1. Go to https://news.ycombinator.com/submit
2. Use the title and URL from `hn-submission.md`
3. After submission, post the Show HN comment from the file

### Reddit (manual)
1. Post to r/programming using the first section from `reddit-posts.md`
2. Post to r/SideProject using the second section
3. Post to r/LocalLLaMA using the third section
4. Post to r/selfhosted using the fourth section

## Tracking

After publishing, update [adoption-metrics-tracking.md](../adoption-metrics-tracking.md) with:
- Publication date per platform
- Post URL per platform
- T+7d and T+14d metric snapshots

Automated metrics via:
- `bin/export_github_evidence.sh` — daily GitHub metrics capture (stars, forks, traffic)
- `bin/publish_case_study.sh` — logs distribution events to `runtime/metrics/distribution_log.jsonl`
- `gh api repos/kai-linux/agent-os/traffic/popular/referrers` — referrer attribution

### Adoption Baseline (2026-04-15)

| Signal | Value |
|---|---|
| GitHub stars | 2 |
| GitHub forks | 0 |
| Unique visitors (14d) | 4 |
| Referrers | github.com only |

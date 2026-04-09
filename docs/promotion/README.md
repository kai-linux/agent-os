# Promotion Content — Agent OS Case Study

Publish-ready drafts for cross-posting the Agent OS multi-agent case study.

## Files

| File | Platform | Status |
|---|---|---|
| [devto-article.md](devto-article.md) | dev.to | Ready to publish (500+ words, includes frontmatter) |
| [hn-submission.md](hn-submission.md) | Hacker News | Ready to submit (title + Show HN comment) |
| [reddit-posts.md](reddit-posts.md) | Reddit r/programming + r/SideProject | Ready to post |

## Publishing Instructions

### dev.to
1. Go to https://dev.to/new
2. Copy the full content of `devto-article.md` (it includes frontmatter)
3. Set `published: true` in the editor
4. Publish

Or use the API with `DEV_API_KEY`:
```bash
curl -X POST https://dev.to/api/articles \
  -H "api-key: $DEV_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"article\": {\"body_markdown\": \"$(cat devto-article.md)\"}}"
```

### Hacker News
1. Go to https://news.ycombinator.com/submit
2. Use the title and URL from `hn-submission.md`
3. After submission, post the Show HN comment from the file

### Reddit
1. Post to r/programming using the title and body from `reddit-posts.md`
2. Post to r/SideProject using the second section

## Tracking

After publishing, check adoption signals:
- GitHub stars/forks trend via `bin/export_github_evidence.sh`
- Referral traffic in GitHub Insights → Traffic

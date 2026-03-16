---
task_id: task-20260313-155027-blog-template-publish
repo: /srv/repos/writeaibook
agent: codex
branch: agent/task-20260313-155027-blog-template-publish
base_branch: main
allow_push: true
attempt: 1
max_attempts: 4
max_runtime_minutes: 40
---

# Goal

Create and publish a new WriteAIBook blog article using the existing template in the blog directory, with content aligned to current KDP/self-publishing demand signals and ready for production publishing flow.

# Success Criteria

- A new article is created from the blog template in the correct blog directory with complete title, metadata, and body content
- The article is integrated into the site’s existing publish workflow so it is published on the WriteAIBook website
- The task includes a brief summary of what was published (title and URL/path) in the task result

# Constraints

- Work only inside the specified repo
- Prefer minimal diffs
- Do not touch secrets, infra, or deployment config unless explicitly asked
- Do not make unrelated changes

# Context

This task is requested as part of the weekly X/KDP analysis workflow. Focus on practical, operator-style content for the WriteAIBook audience (KDP, self-publishing, Kindlepreneur, AI-assisted workflows) and use the existing blog template in the repo’s blog directory.
# GitHub Visitor-to-Star Conversion Funnel Analysis

> Date: 2026-04-15 | Window: 14 days (2026-04-01 to 2026-04-14)

## Traffic Summary

| Metric | Value |
|---|---|
| Page views | 60 (4 unique) |
| Clones | 2,177 (591 unique) |
| Stars | 2 (unchanged since 2026-03-18) |
| Forks | 0 |
| Watchers | 0 |
| External referrers | 0 |
| Community health | 57% |

## Funnel Breakdown

```
Discovery (external) ──→ Landing (README) ──→ Engagement ──→ Star/Fork
     ~0 external              4 uniques         ~1-2           0 new
     referrers               in 14 days        (projects,     in 28 days
                                               pulls pages)
```

**The funnel is empty at the top.** The conversion problem is not README
quality or activation friction — it's that almost nobody is arriving.

### Clone Volume Is Self-Traffic

The 591 "unique cloners" are almost entirely the CI/automation system:
- Worktree operations (agent dispatch creates git clones)
- 248 clones on Apr 14 alone = automated task execution
- Actual human cloners: likely 0-2 (matching the 4 unique viewers)

### Star Timeline

Both stars were acquired on March 17-18, 2026 (the first two days of the
project). Zero new stars in the 28 days since, despite:
- README rewrite (condensed to 1-page pitch)
- Demo SVG added
- Performance metrics embedded
- Quickstart section added
- Reliability dashboard published
- Case study written
- Promotion content drafted
- 20 topics added
- Repository description optimized

**None of these improvements can convert visitors who don't exist.**

## Top 3 Friction Barriers (ranked by impact)

### 1. Zero Distribution — No External Traffic (Critical)

**Impact:** Blocks all downstream conversion. No visitors = no stars.

**Evidence:**
- 0 external referrers in GitHub traffic data
- Promotion content (dev.to article, HN submission, Reddit posts) was created
  but never published
- `DEV_API_KEY` is not set — automated publishing was skipped
- HN and Reddit submissions require manual posting — never done
- No backlinks from any external site
- GitHub search/trending requires external signals that don't exist

**Why this is #1:** Every other optimization (README, demo, quickstart,
dashboard) is wasted effort until people actually see the repo. The team
has spent ~40% of sprint capacity on adoption work that optimizes a funnel
with zero input.

### 2. Social Proof Deficit — Cold Start Problem

**Impact:** Even if traffic arrives, 2 stars / 0 forks signals "unvalidated."

**Evidence:**
- 2 stars (owner/team, not organic)
- 0 forks, 0 watchers
- Discussion #167 has 0 comments
- No external mentions, blog posts, or citations found
- Shields.io badges show "2" which may deter rather than attract

**Why this matters:** Technical builders use star count as a credibility
proxy. A repo with 2 stars and 200+ commits looks like a personal project
that nobody uses, regardless of how good the README is. There's a minimum
viable social proof threshold (~50-100 stars) below which most visitors
won't engage.

### 3. Activation Complexity for Target Audience

**Impact:** Moderate — affects the few visitors who do arrive.

**Evidence:**
- "5-minute" quickstart requires: git, python3, venv, pip, gh CLI auth,
  GitHub Projects board, config.yaml editing, understanding of worktree
  model
- Real time-to-first-task: 15-30 minutes for someone unfamiliar
- demo.sh requires claude CLI (paid tool) — not available to casual
  evaluators
- No hosted demo, playground, or output gallery
- No Docker/one-line setup option

**Why this is #3:** Activation friction matters less when traffic is near
zero. Fix #1 first, then optimize activation for the visitors who arrive.

## Proposed Fixes (highest-impact barrier first)

### Fix for Barrier #1: Distribute the Content That Already Exists

The promotion content is written and ready. The bottleneck is publishing.

**Immediate actions (automatable):**
1. Pin Discussion #167 so it's visible on the repo page
2. Set `DEV_API_KEY` and run `bin/publish_case_study.sh` to post to dev.to
3. Submit the HN Show HN post manually (no API)
4. Post to r/programming and r/SideProject manually

**Medium-term actions:**
5. Write a personal blog post linking to the repo (backlink for SEO)
6. Share on Twitter/X, Mastodon, LinkedIn with the demo GIF
7. Comment on relevant threads about autonomous agents, CI/CD automation
8. Submit to newsletters: TLDR, AI Weekly, DevOps Weekly

**Expected impact:** Even modest distribution (1 HN post + 1 dev.to article +
2 Reddit posts) typically generates 500-2000 views in the first week. At a
conservative 1-2% star conversion rate, that's 5-40 new stars.

### Fix for Barrier #2: Minimum Viable Social Proof

**After traffic arrives:**
1. Add "Star this repo" CTA in the README near the top
2. Pin an issue asking for feedback (signals active development)
3. Add an "awesome-list" entry for relevant lists
4. Cross-reference from other repos if applicable

### Fix for Barrier #3: Lower Activation Barrier

**After traffic and social proof:**
1. Add a Dockerfile for one-command setup
2. Create an output gallery (screenshots of real PRs, issues, merged code)
3. Consider a hosted demo environment or video walkthrough
4. Remove claude CLI dependency from demo.sh (offer a mock mode)

## Implemented Fix

Pinned Discussion #167 for immediate repo page visibility. Added a
star-request CTA to the README. These are zero-cost, zero-risk changes
that improve conversion for the small amount of existing traffic while
the distribution bottleneck is addressed.

## Measurement Plan

Track over 7 days after distribution:
- GitHub traffic views/uniques (should increase from baseline 4 → 50+)
- External referrers (should appear: dev.to, reddit.com, news.ycombinator.com)
- Star count (target: +5-10 stars in first week post-distribution)
- Clone uniques (filter out automated clones by comparing with task dispatch count)

## Conclusion

The repo has invested heavily in conversion optimization (README, demo,
quickstart, dashboard, case study) but has not distributed any of it.
**The highest-leverage action is not another README edit — it's posting the
content that already exists to the channels where the target audience lives.**

Until external traffic sources exist, all README/activation optimization
has approximately zero impact on star growth.

# Video Walkthrough — Production Script

Target length: 3–5 minutes. Audience: technical builders evaluating autonomous
software organization frameworks. Tone: builder-to-builder, no marketing.

## Demo Subject

A real, publicly-auditable task that already ran end-to-end on this repo:

- **Issue:** [#115 — Cluster CI failures by error signature to deduplicate debug work](https://github.com/kai-linux/agent-os/issues/115)
- **PR:** [#122](https://github.com/kai-linux/agent-os/pull/122) — 567 additions, 8 deletions, 5 files
- **Dispatch → merge:** ~3 hours (2026-04-01 08:01 UTC → 11:10 UTC)
- **Outcome:** CI green, auto-merged, issue auto-closed

This pair is already featured in the README's "See it work" section, so the
video reinforces the existing entry-point narrative instead of introducing a
new one.

## Pre-Recording Checklist

- [ ] Clean shell prompt, large font (18pt+), dark theme.
- [ ] Screen recorder set to 1080p or higher, 30fps, with microphone input.
- [ ] Browser window with the following tabs prepared in order:
  1. `https://github.com/kai-linux/agent-os/issues/115` (the source issue)
  2. `https://github.com/kai-linux/agent-os/pull/122/files` (the diff)
  3. `https://github.com/kai-linux/agent-os/actions` (CI runs)
  4. `https://github.com/kai-linux/agent-os/pulls?q=is%3Apr+is%3Amerged` (merged PR list)
  5. `docs/reliability/README.md` on GitHub (live dashboard)
- [ ] Terminal open in the agent-os repo root.
- [ ] Quiet room, do a 10-second audio test before the real take.

## Shot List and Narration

### 0:00–0:20 — Cold open: the backlog-to-PR claim

**Screen:** README top section (the "You give it a backlog. It ships product."
line and the demo SVG).

**Narration:**
> Agent OS is an autonomous software organization. You file a GitHub issue,
> an agent picks it up, writes the code, opens a PR, waits for CI, and
> merges. I'm going to show you a real task that went through this pipeline
> three weeks ago — no edits, no replays.

### 0:20–0:50 — The input: a real issue

**Screen:** Issue #115 on GitHub. Scroll through the Goal and Success Criteria.

**Narration:**
> This is issue #115. "Cluster CI failures by error signature to deduplicate
> debug work." It's a non-trivial feature — about 500 lines of code across
> five files, with real success criteria. I filed it as a normal GitHub issue
> with a `prio:high` label. No prompts, no scripts, no special format.

**Caption to overlay:** `Filed 2026-04-01 08:01 UTC`

### 0:50–1:30 — The dispatcher picks it up

**Screen:** Terminal — run and show:

```bash
bin/agentos status --tail 20
```

Then show the relevant log entry (or use `git log --oneline -- orchestrator/github_dispatcher.py | head -5`
to point at the dispatcher code).

**Narration:**
> Every 60 seconds, the dispatcher polls GitHub, ranks open issues by priority
> and age, runs each one through an LLM to format a task file, and routes it
> to the right agent based on task type and recent per-agent success rates.
> For issue #115, it picked Claude because this was an implementation task
> and Claude had the highest 14-day success rate on implementation work.

**Caption:** `Dispatcher → Queue → Worktree → Agent → .agent_result.md`

### 1:30–2:20 — The agent does the work

**Screen:** PR #122 "Files changed" tab. Scroll through the diff slowly:
the new clustering module, the tests, the queue integration.

**Narration:**
> The agent ran in an isolated git worktree, read the repo's CLAUDE.md and
> codebase memory, wrote the clustering module with tests, ran the full test
> suite locally, and produced a structured result file. Here's the diff: a
> new signature extractor, clustering logic in the queue, and a test module
> with twelve cases. All of it auditable, all of it shipped by the agent.

**Caption:** `567 additions · 5 files · test coverage included`

### 2:20–2:50 — CI and auto-merge

**Screen:** Actions tab → show the CI run for PR #122 (all green). Then the
PR conversation view showing the "Merged" banner and the "Closes #115" link.

**Narration:**
> The PR monitor runs every five minutes. It waits for CI to go green, runs
> a pre-merge work verifier to catch stub implementations or scope escapes,
> then squash-merges and closes the linked issue. No human in the loop for
> the happy path — but every step is logged to an append-only audit chain
> that you can inspect after the fact.

**Caption:** `Merged 2026-04-01 11:10 UTC · ~3 hours end-to-end`

### 2:50–3:30 — The closed loop: this is the part that matters

**Screen:** Reliability dashboard (`docs/reliability/README.md`), then scroll
down to Top Blocker Categories. Then show `STRATEGY.md` sprint history.

**Narration:**
> Here's what separates Agent OS from a task runner. Every merged PR, every
> CI failure, every blocker code gets fed back into production feedback. The
> log analyzer files remediation tickets for its own failures. The strategic
> planner uses that evidence to pick the next sprint. When the system breaks,
> it diagnoses itself and files the fix — and an agent picks that up too.
> It's turtles all the way down.

### 3:30–4:20 — What this saves you

**Screen:** Split view or overlay of two columns.

**Caption left (manual):**
- Triage issue
- Write code
- Write tests
- Open PR
- Wait for CI
- Review diff
- Merge
- File follow-ups
- ~2–4 hours of focused time per task

**Caption right (Agent OS):**
- File issue
- (go do something else)
- Review merge notification
- ~5 minutes of human time per task

**Narration:**
> For issue #115 specifically: three hours of wall-clock time, roughly five
> minutes of my attention. The rest — dispatch, coding, tests, CI, merge,
> issue closure — ran autonomously. Across the last 14 days on this repo
> that's 85 merged PRs at a 69% success rate, with mean completion time
> measured in minutes, not hours.

**Caption:** `Capabilities unlocked: parallel execution across repos · evidence-driven sprint planning · self-healing via the log analyzer`

### 4:20–4:50 — How to try it

**Screen:** README quickstart section.

**Narration:**
> If you want to run this on your own repo, start in dispatcher-only mode
> with manual PR review. Give it five to ten well-specified issues. Measure
> escalation rate and merge quality before you give it the full loop. The
> fork guide in the repo root walks through the customization points. Links
> in the description.

### 4:50–5:00 — Close

**Screen:** README top, reliability dashboard link visible.

**Narration:**
> Everything you saw here is public, auditable, and reproducible. Issue, PR,
> CI run, merge commit, reliability numbers — all linked in the description.
> Thanks for watching.

## Post-Production Checklist

- [ ] Trim silence and umm/uhh. Keep total length under 5:00.
- [ ] Add lower-third captions at each shot-list caption mark.
- [ ] Pin the "See it work" README section in the first frame of the thumbnail.
- [ ] Export at 1080p, H.264, target 50–150 MB.
- [ ] Upload to YouTube (unlisted first for review, then public) and/or Loom.
- [ ] Description should include every linked issue/PR/dashboard URL from
      this script, plus the repo URL.

## After Upload — Links to Add

Once the video URL is live, make the following small diffs (the task body
says "prefer minimal diffs"):

### 1. `README.md`

Under the existing "See it work - real task, end-to-end execution" heading
(around line 16), add **one line** after the existing caption paragraph:

```markdown
▶ [Video walkthrough (3–5 min)](<VIDEO_URL>) — same task, narrated end-to-end.
```

### 2. `EXTERNAL_PROOF.md`

This artifact does not yet exist. Create it at the repo root with the
following seed content (the video launch is a good trigger to stand it up):

```markdown
# External Proof

Public, auditable evidence that Agent OS ships real work on real repos.

## Video Walkthroughs

| Title | Length | Date | Demo Subject |
|---|---|---|---|
| [Agent OS end-to-end: issue to merge](<VIDEO_URL>) | ~5 min | <YYYY-MM-DD> | Issue #115 → PR #122 |

## Live Artifacts

- [Reliability dashboard](docs/reliability/README.md) — 14-day rolling metrics
- [Case study](docs/case-study-agent-os.md) — 30-day bootstrap timeline
- [Merged PRs](https://github.com/kai-linux/agent-os/pulls?q=is%3Apr+is%3Amerged) — every shipped change
- [Closed issues](https://github.com/kai-linux/agent-os/issues?q=is%3Aissue+is%3Aclosed) — every completed task
```

Keep the diff to README minimal (one line). Keep EXTERNAL_PROOF.md seed
small — it can grow as more proof lands.

## Post-Launch Tracking

Per the parent task, track these outcome checks for 14 days after publish:

- `agent_success_rate` — dashboard baseline is 69% (2026-04-23)
- `escalation_rate` — dashboard baseline is 17% (2026-04-23)
- `task_completion_time` — dashboard baseline is 0.1h mean (2026-04-23)

Also log the video URL and publish date to
`runtime/metrics/distribution_log.jsonl` via `bin/publish_case_study.sh`
or the existing adoption-metrics-tracking flow.

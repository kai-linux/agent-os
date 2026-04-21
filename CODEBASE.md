# Codebase Memory

> Auto-maintained by agent-os. Agents read this before starting work and update it on completion.

## Architecture

(Fill in once the project structure stabilises. Agents will append discoveries below.)

## Key Files

(Agents append important file paths and their purpose here.)

## Known Issues / Gotchas

### PR-98 Cascading CI Failure Pattern (RCA — 2026-04-08)

**Root cause:** The CI completion verification gate (`verify_pr_ci_debug_completion` in `queue.py`) extracted failed CI job names from markdown prose in issue bodies using `_extract_ci_failed_checks()`. When follow-up tasks were created, issue body reformatting stripped the `- **jobname**: \`...\`` lines, causing `_extract_ci_failed_checks` to return an empty list. The gate then downgraded otherwise-successful tasks to `partial` with `CI_RERUN_REASON=missing_failed_job_context`, which spawned new follow-up debug tasks that repeated the cycle.

**Error signature:** `CI remediation completion gate downgraded task to partial (missing_failed_job_context)` in queue logs, despite agent's local tests passing.

**Reproduction:** Create a CI debug task → agent fixes the issue → follow-up reformats the issue body → `_extract_ci_failed_checks(body)` returns `[]` → task marked partial → new follow-up spawned → repeat.

**Fix applied:** Persist `failed_checks` as a structured list in task frontmatter at dispatch time (`github_dispatcher.py:build_mailbox_task`). The verification gate now reads `meta["failed_checks"]` first, falling back to body parsing only when the structured field is absent. Follow-up task creation (`queue.py:create_followup_task`) propagates the `failed_checks` field from the original task's frontmatter.

**Impact:** 8+ cascading debug tasks from PR-98 (issues #99, #102–106, #109, #111, #117); similar pattern on PR-119 (#120). Fix prevents metadata loss across all future CI debug follow-up chains.

### Codex Agent Runtime Degradation (RCA — 2026-04-09, validated 2026-04-14)

**Root cause:** Codex was scored as a single blended bucket across all task types. Debugging-path routing decisions were polluted by unrelated implementation task failures (mostly `missing_context`), artificially depressing codex's apparent success rate to ~56%. The health gate then over-aggressively benched codex based on this blended rate.

**Error signature:** Codex gated or flagged as degraded despite succeeding on its target task types. All-time rate 61.5% (40/65), with 23 of 25 failures attributed to `missing_context` — predominantly on debugging tasks pre-fix.

**Fix applied (task-20260409-210219):**
1. Task-type-aware success rates in `agent_scorer.py:compute_success_rates()` — accepts optional `task_type` filter so codex debugging performance is measured separately from implementation.
2. Scoped health gate in `filter_healthy_agents()` — uses task-type-specific rates when agent has >= `min_task_count` samples for that type, falls back to overall rate otherwise.
3. Small-sample protection — 24h strict gate requires >= 5 tasks before filtering an agent, preventing false positive benching from 1-2 failures.
4. `build_degradation_findings()` prefers "debugging" slice for codex analysis.

**Validation (2026-04-14):** Post-fix codex: 2/2 (100%) success. 14-day rate: 6/8 (75%), but 2 failures were pre-fix debugging tasks. The health gate correctly routes codex to task types where it succeeds. Sample size is small (n=2 post-fix) so continued monitoring via the "Codex Runtime Stability" section in `HEALTH_GATE_REPORT.md` is essential.

**Regression prevention:** Tests in `test_agent_scorer.py` validate: task-type-scoped gating, small-sample protection, and degradation finding scoping. `health_gate_report.py` includes a dedicated "Codex Runtime Stability" monitoring section that warns when codex drops below 85%.

## Recent Changes

### 2026-04-21 — [task-20260421-130125-create-fork-guide-md-with-customization-patterns] (#208 kai-linux/agent-os)
Added FORK_GUIDE.md at the repo root covering the high-leverage customization entry points solo builders reach for when forking Agent OS — agent routing and fallback chains, adding a new agent (runner + registration + pricing), task dispatch and task_type extension, prompt assembly and context injection, objectives and scoring files, per-repo overrides, and the pre-commit secret guard. The guide is example-driven with concrete config snippets, file:line references to the real code paths, and a small "what NOT to fork" section to steer users away from invasive changes. Also linked the new guide from README.md's Documentation table so it is discoverable from the front door.

**Files:** `- FORK_GUIDE.md`, `- README.md`, `- .agent_result.md`

**Decisions:**
  - - Kept the guide at repo root (FORK_GUIDE.md) rather than docs/fork-guide.md because CONTRIBUTING.md / NORTH_STAR.md / README.md already live at root and the fork audience lands on the root listing first
  - - Focused on the three entry points called out in the task (routing, dispatch, prompts) plus the closely-adjacent ones a forker always needs (objectives, per-repo overrides, pre-commit). Skipped deep-dive topics already covered in docs/configuration.md and docs/execution.md and linked to them instead
  - - Added a "What NOT to fork" section to protect the .agent_result.md contract, CODEBASE.md format, mailbox layout, and Project status field semantics — changing these cascades through many modules and is a common fork trap
  - - Verified all referenced file paths, line numbers, and linked docs exist before writing


### 2026-04-21 — [task-20260421-122231-phase-1-authenticated-web-dashboard-auth-layer-tai] (#266 kai-linux/agent-os)
Implemented a new dashboard auth layer with Tailscale and shared-secret backends, enforced safe bind-address startup validation, allowed unauthenticated reads only on localhost, required authentication for writes, and logged authenticated write actions to the immutable audit trail.

**Files:** `- .agent_result.md`, `- example.config.yaml`, `- orchestrator/dashboard/__init__.py`, `- orchestrator/dashboard/auth.py`, `- orchestrator/paths.py`, `- tests/test_dashboard_auth.py`, `- tests/test_paths.py`

**Decisions:**
  - - Kept the implementation framework-agnostic inside a new `orchestrator/dashboard/auth.py` helper so the eventual Flask/Quart dashboard can reuse one auth contract with minimal integration churn.
  - - Enforced the non-local bind guard inside `load_config()` so unsafe dashboard exposure fails during startup rather than waiting for a later route initialization path.


### 2026-04-21 — [task-20260421-122140-add-system-architect-agent-for-capability-sensor-g] (#249 kai-linux/agent-os)
Implemented a new deterministic system architect loop that compares current agent-os capabilities and sensors against a required operator-curated `target_operating_model.yaml`, emits `capability_gap` and `sensor_gap` findings through the existing scorer artifact, gates architect-generated backlog issues behind the existing Telegram approval flow, and surfaces the latest architect state in the daily digest.

**Files:** `- .agent_result.md`, `- example.config.yaml`, `- orchestrator/agent_scorer.py`, `- orchestrator/backlog_groomer.py`, `- orchestrator/daily_digest.py`, `- orchestrator/system_architect.py`, `- target_operating_model.yaml`, `- tests/test_backlog_groomer.py`

**Decisions:**
  - - Reused the existing scorer artifact and `plan:*` Telegram callback path instead of inventing a second approval or findings transport, so architect gaps flow through the same control-plane evidence surface and approval semantics as the planner.
  - - Stored accepted omissions in the operator-curated `target_operating_model.yaml` rather than a hidden runtime suppression list, keeping omissions explicit, reviewable, and versioned alongside the target operating model itself.
  - - Made the architect cadence default monthly in module logic while still allowing scorer runs in between to reuse the latest persisted architect report, so findings stay visible without recomputing topology every week.


### 2026-04-21 — [task-20260421-122034-add-severity-tiered-incident-response-escalation-l] (#248 kai-linux/agent-os)
Implemented explicit per-tier incident handler configuration on top of the existing severity router, keeping sev1 immediate paging and sev2/sev3 dedup behavior while allowing Telegram routing to be configured per severity tier.

**Files:** `- .agent_result.md`, `- example.config.yaml`, `- orchestrator/incident_router.py`, `- orchestrator/paths.py`, `- orchestrator/queue.py`, `- tests/test_incident_router.py`

**Decisions:**
  - - Kept the existing `delivery` and `dedup_window_minutes` fields for backwards compatibility and layered the requested `handlers`/`snooze_minutes` config on top instead of rewriting routing semantics.
  - - Extended `queue.send_telegram()` with optional `chat_id` and `bypass_kill_switch` parameters so incident routing can target per-tier handlers without changing older call sites.


### 2026-04-21 — [task-20260421-121937-replace-telegram-actions-log-with-hash-chained-imm] (#247 kai-linux/agent-os)
Implemented a hash-chained immutable audit log with rotation and verification, routed the requested mutating Telegram/GitHub flows through it, added daily digest chain-status reporting plus tamper alerts, and covered the new behavior with focused regression tests.

**Files:** `- .agent_result.md`, `- bin/audit_verify`, `- orchestrator/audit_log.py`, `- orchestrator/backlog_groomer.py`, `- orchestrator/daily_digest.py`, `- orchestrator/deploy_watchdog.py`, `- orchestrator/log_analyzer.py`, `- orchestrator/pr_monitor.py`

**Decisions:**
  - - Hashed the full immutable event body (`ts`, `event_type`, `payload`) rather than payload alone so timestamp and event type are also integrity-protected while keeping the required stored fields.
  - - Used file rewrite via temp file plus `os.replace` for both the audit log and manifest so appends stay atomic and partial writes do not leave half-written JSONL.
  - - Kept rotation state in `runtime/audit/manifest.json` with ordered `(file, last_hash)` entries so verification can detect truncated tails after rotation as well as in the active file.
  - - Made `bin/audit_verify` fall back to `python3` when `.venv/bin/python3` is absent because this worktree does not carry a repo-local virtualenv.


### 2026-04-21 — [task-20260421-121837-add-adr-curator-extracting-architecture-decisions-] (#246 kai-linux/agent-os)
Implemented an append-only ADR curator that captures qualifying merged PRs into per-repo `docs/adrs/`, updates the planner prompt with recent ADR context, hooks ADR capture into the merge path, and adds focused regression coverage.

**Files:** `- orchestrator/adr_curator.py`, `- orchestrator/pr_monitor.py`, `- orchestrator/strategic_planner.py`, `- tests/test_adr_curator.py`, `- tests/test_strategic_planner.py`, `- .agent_result.md`

**Decisions:**
  - - Reused the existing completed ADR-curator change set from commit `34b19c3` as the implementation source, but applied it manually because the sandbox denied `git cherry-pick` index writes in this worktree.
  - - Kept ADR generation deterministic from merged PR metadata, labels, and architectural-surface file heuristics instead of introducing an LLM dependency into the recurring merge and planning control path.
  - - Deduplicated ADRs with a stable `<!-- adr-source: owner/repo#PR -->` marker so reruns stay append-only and never rewrite an existing ADR file.


### 2026-04-21 — [task-20260421-121730-add-adr-curator-extracting-architecture-decisions-] (#288 kai-linux/agent-os)
Implemented an ADR curator that extracts architectural decisions from qualifying merged PRs into per-repo `docs/adrs/`, maintains `docs/adrs/INDEX.md`, hooks the curator into the post-merge PR monitor flow, and injects recent ADR context into the strategic planner prompt.

**Files:** `- .agent_result.md`, `- orchestrator/adr_curator.py`, `- orchestrator/pr_monitor.py`, `- orchestrator/strategic_planner.py`, `- tests/test_adr_curator.py`, `- tests/test_strategic_planner.py`

**Decisions:**
  - - Kept ADR generation deterministic and file-based from PR metadata and changed-file heuristics instead of adding an LLM summarization dependency to a recurring control-plane path.
  - - Used an inline post-merge hook in `pr_monitor` plus a standalone daily CLI entrypoint in `orchestrator/adr_curator.py` to satisfy both immediate capture and scheduled backfill with minimal integration churn.
  - - Deduplicated ADRs by embedding a stable `<!-- adr-source: owner/repo#PR -->` marker so reruns stay append-only and never rewrite existing ADR entries.


### 2026-04-21 — [task-20260421-121633-add-severity-tiered-incident-response-escalation-l] (#285 kai-linux/agent-os)
Implemented a shared severity-tiered incident router with configurable source rules, persistent incident tracking, `/ack` and `/resolve` commands, and routing integrations for queue, pr_monitor, agent_scorer, and deploy_watchdog while preserving existing Telegram button workflows.

**Files:** `- .agent_result.md`, `- example.config.yaml`, `- orchestrator/agent_scorer.py`, `- orchestrator/deploy_watchdog.py`, `- orchestrator/incident_router.py`, `- orchestrator/paths.py`, `- orchestrator/pr_monitor.py`, `- orchestrator/queue.py`

**Decisions:**
  - - Kept the router file-based and JSONL-backed so incidents stay auditable and consistent with the rest of the orchestrator runtime artifacts.
  - - Preserved existing Telegram callback/button flows by letting incident events carry optional `reply_markup` through the router instead of replacing the action store.
  - - Made severity classification rule-based from source/event metadata with conservative defaults (`sev3` unless configured otherwise) so nothing becomes sev1 without explicit opt-in.
  - - Stored incidents under `root_dir/runtime/incidents/incidents.jsonl` directly rather than depending on the module-level repo root, which keeps tests and multi-root configs deterministic.


### 2026-04-21 — [task-20260421-094629-add-deploy-watchdog-agent-that-auto-reverts-post-m] (#245 kai-linux/agent-os)
Implemented an opt-in deploy watchdog that scans recent merged PRs against external production telemetry, opens labeled revert PRs on regression, routes operator approval through Telegram buttons, and logs both detection and operator decisions to a dedicated JSONL audit trail.

**Files:** `- .agent_result.md`, `- CRON.md`, `- example.config.yaml`, `- bin/run_deploy_watchdog.sh`, `- orchestrator/deploy_watchdog.py`, `- orchestrator/queue.py`, `- tests/test_deploy_watchdog.py`, `- tests/test_queue.py`

**Decisions:**
  - - Reused the existing `outcome_attribution.jsonl`, `external_signals.jsonl`, Telegram action store, and `gh` helpers so the watchdog stays file-based and auditable instead of introducing a parallel control path.
  - - Kept the watchdog opt-in per repo with fixed 10-minute cron cadence and configurable regression ratios (`2.0x` error rate, `1.5x` latency p95) to match the requested operating model with minimal surface area.


### 2026-04-21 — [task-20260421-094524-build-slo-state-tracker-module] (#260 kai-linux/agent-os)
Built a new `orchestrator/slo_tracker.py` module that loads opt-in repo SLO YAML files, computes success-rate and merge-cycle burn metrics from runtime telemetry while excluding transient blocker codes from success-rate violations, and rewrites `runtime/metrics/slo_state.jsonl` with standardized daily state rows.

**Files:** `- orchestrator/slo_tracker.py`, `- tests/test_slo_tracker.py`, `- .agent_result.md`

**Decisions:**
  - - Imported `_TRANSIENT_BLOCKER_CODES` from `agent_scorer` so SLO success-rate calculations stay aligned with the existing health-gate definition of transient failures.
  - - Kept the implementation file-based and rebuild-oriented, matching adjacent metrics modules by reading `agent_stats.jsonl` and `outcome_attribution.jsonl` and atomically rewriting `runtime/metrics/slo_state.jsonl`.


### 2026-04-21 — [task-20260421-094340-add-dependency-cve-watcher-with-auto-bump-prs-and-] (#243 kai-linux/agent-os)
Implemented a new dependency and CVE watcher module with weekly per-repo cadence gating, dispatcher-only skips, bounded weekly action limits, high-risk issue escalation, and low-risk npm devDependency patch auto-PR support, plus focused regression coverage and config examples.

**Files:** `- .agent_result.md`, `- example.config.yaml`, `- orchestrator/dependency_watcher.py`, `- tests/test_dependency_watcher.py`

**Decisions:**
  - - Reused `scheduler_state` and `repo_modes` instead of threading the watcher through planner/groomer, keeping the feature as a standalone scheduled job with the same cadence semantics as the rest of the orchestrator.
  - - Scoped automatic edits to npm `package.json` devDependency patch bumps in this first version; higher-risk findings and unsupported auto-edit cases escalate through GitHub issues instead of making unsafe manifest changes.


### 2026-04-21 — [task-20260421-094236-add-external-signal-ingester-for-production-teleme] (#242 kai-linux/agent-os)
Added a pluggable external-signal ingester with opt-in source adapters, normalized JSONL persistence, and prompt integration so recent production/support/public signals now flow into both backlog grooming and planner evidence.

**Files:** `- .agent_result.md`, `- example.config.yaml`, `- orchestrator/backlog_groomer.py`, `- orchestrator/external_ingester.py`, `- orchestrator/strategic_planner.py`, `- tests/test_backlog_groomer.py`, `- tests/test_external_ingester.py`, `- tests/test_strategic_planner.py`

**Decisions:**
  - - Reused the existing `PRODUCTION_FEEDBACK.md` artifact as the shared planner/groomer evidence surface and added external-signal sections instead of introducing a second prompt artifact
  - - Kept all adapters opt-in and rate-limited via repo runtime state so planner and groomer can safely call the ingester without burning API quota


### 2026-04-21 — [task-20260421-094128-implement-cost-tracking-infrastructure] (#253 kai-linux/agent-os)
Implemented foundational cost tracking by persisting per-attempt model metadata into `agent_stats.jsonl`, rebuilding `runtime/metrics/cost_records.jsonl` from a new `orchestrator/cost_tracker.py` module, and documenting/configuring the pricing assumptions and override knobs.

**Files:** `- .agent_result.md`, `- docs/cost-tracking.md`, `- example.config.yaml`, `- orchestrator/cost_tracker.py`, `- orchestrator/queue.py`, `- tests/test_cost_tracker.py`, `- tests/test_queue.py`

**Decisions:**
  - - Kept cost tracking file-based and rebuilt from `agent_stats.jsonl` so there is still a single source of runtime telemetry truth
  - - Used rough `chars / 4` token estimates from prompt/result artifacts because governance-grade approximation is acceptable and raw provider usage is not persisted today
  - - Stored task, repo, and global cost rows in one JSONL file via `record_type` fields so downstream consumers can read one artifact


### 2026-04-21 — [task-20260421-093931-attach-goal-ancestry-objective-sprint-issue-to-eve] (#240 kai-linux/agent-os)
Implemented optional goal ancestry fields through dispatch, prompt rendering, escalations, and generated issue creation so agents and humans can see objective, sprint, and parent-issue context without breaking tasks that lack ancestry.

**Files:** `- orchestrator/task_formatter.py`, `- orchestrator/github_dispatcher.py`, `- orchestrator/queue.py`, `- orchestrator/backlog_groomer.py`, `- orchestrator/log_analyzer.py`, `- tests/test_github_dispatcher.py`, `- tests/test_queue.py`, `- tests/test_log_analyzer.py`

**Decisions:**
  - - Centralized ancestry resolution/rendering in `task_formatter.py` so dispatcher, generators, and escalation paths reuse one bounded format
  - - Derived `objective_id` from the objective file path when no explicit id exists, preserving current objective schema
  - - Used `runtime/next_sprint_focus.json` as the active sprint source and derived `sprint_id` from its `generated_at` date
  - - Persisted ancestry into `agent_stats.jsonl` so log-analyzer tickets can inherit ancestry from the failure records they summarize


### 2026-04-21 — [task-20260421-093841-add-quality-harness-architect-with-modality-detect] (#250 kai-linux/agent-os)
Implemented a quality-harness architect flow with repo modality detection, opt-in eval-suite planning and merge gating, plus a Telegram/GitHub field-failure capture path that writes permanent regression fixtures.

**Files:** `- .agent_result.md`, `- example.config.yaml`, `- orchestrator/backlog_groomer.py`, `- orchestrator/pr_monitor.py`, `- orchestrator/quality_harness.py`, `- orchestrator/queue.py`, `- tests/test_quality_harness.py`

**Decisions:**
  - - Reused repo-local runtime artifacts and Telegram action files instead of inventing a second approval or fixture-ingestion subsystem
  - - Made the merge gate opt-in via explicit `quality_harness.enabled + suites` so non-opted-in repos keep the existing pytest-only behavior
  - - Enforced the operator gate by filtering quality-harness implementation issues in the groomer until `operator_approved` is set
  - - Stored incomplete field failures under `tests/fixtures/unverified/...` so ambiguous reports do not silently become merge-blocking fixtures


### 2026-04-15 — [task-20260415-120520-complete-pr-165-ci-failure-debug-and-prevent-casca] (#186 kai-linux/agent-os)
PR-165 was already merged with all CI checks passing (SUCCESS). The PR-98 cascading-CI fix (persistent failed_checks in frontmatter) worked correctly: only 2 follow-up tasks were spawned (#166, #170) before the chain terminated, compared to 8+ cascading tasks in the pre-fix PR-98 incident. Added a regression test for failed_checks propagation through follow-up task creation to complete test coverage of the anti-cascade mechanism.

**Files:** `- tests/test_queue.py`

**Decisions:**
  - - No code fix needed — PR-165 was already merged with green CI; this task validates closure and prevention
  - - Added follow-up propagation test as the missing coverage gap — the verification gate test existed but propagation through create_followup_task was untested
  - - No PR-98 gap found — the fix correctly prevents cascading follow-ups (2 follow-ups vs 8+ pre-fix)


### 2026-04-15 — [task-20260415-120419-implement-adoption-funnel-monitoring-and-weekly-im] (#206 kai-linux/agent-os)
Implemented adoption funnel monitoring with daily-granularity traffic data capture and a weekly impact report generator. Enhanced the evidence exporter to capture daily views/clones breakdown, referrer details, and popular paths. Created orchestrator/adoption_report.py that fetches live GitHub traffic data, correlates referral sources with conversion rates, identifies conversion bottlenecks (top-of-funnel traffic and referral diversity), and generates a 1-page actionable report. Generated the first report showing 4 unique visitors, 2 stars, zero external referrers, and clone noise from automated operations. Installed weekly cron and sent Telegram summary.

**Files:** `- orchestrator/adoption_report.py`, `- bin/run_adoption_report.sh`, `- bin/export_github_evidence.sh`, `- docs/adoption-reports/adoption-report-2026-04-15.md`, `- docs/adoption-reports/WEEKLY_ADOPTION_REPORT.md`, `- CRON.md`, `- .agent_result.md`

**Decisions:**
  - - Used live GitHub API calls in the report generator rather than only reading from evidence JSONL, ensuring fresh data even if the evidence exporter hasn't run recently
  - - Wrote both a dated report (adoption-report-YYYY-MM-DD.md) and a latest symlink (WEEKLY_ADOPTION_REPORT.md) so historical reports are preserved while the latest is always at a stable path
  - - Scheduled cron at 07:30 UTC Monday (after log analyzer at 07:00) to keep the weekly cadence aligned with existing monitoring jobs
  - - Identified clone traffic as noise (591 unique cloners dominated by automated worktree operations) and explicitly flagged it as non-adoption-signal in the report
  - - Kept bottleneck identification rule-based rather than LLM-based to avoid model costs on a weekly recurring job


### 2026-04-15 — [task-20260415-120322-complete-multi-agent-case-study-distribution-and-m] (#205 kai-linux/agent-os)
Completed multi-agent case study distribution preparation by updating all promotion content with current metrics (352 commits, 110 issues closed, 85 PRs merged, 91% 14-day success rate), expanding target communities from 3 to 7 platforms (dev.to, HN, r/programming, r/SideProject, r/LocalLLaMA, r/selfhosted, plus existing GitHub Discussions #167), establishing a baseline metrics snapshot (2 stars, 0 forks, 4 unique visitors), and creating an adoption metrics tracking framework with 7/14-day measurement windows, platform ROI analysis template, and referrer attribution methodology.

**Files:** `- docs/case-study-agent-os.md`, `- docs/promotion/devto-article.md`, `- docs/promotion/hn-submission.md`, `- docs/promotion/reddit-posts.md`, `- docs/promotion/README.md`, `- docs/adoption-metrics-tracking.md`, `- bin/publish_case_study.sh`

**Decisions:**
  - - Updated all promotion content from 23/27-day stale metrics to current 30-day numbers (352 commits, 110 issues closed, 85 PRs merged) for accuracy and credibility
  - - Expanded from 3 target platforms to 7 by adding r/LocalLLaMA (multi-agent audience) and r/selfhosted ($5 VPS angle) as high-signal technical communities
  - - Each Reddit post uses contextual framing specific to the subreddit audience rather than identical cross-posts
  - - Highlighted the 61%→91% success rate improvement arc as the leading metric in all promotion content because it demonstrates the self-improvement loop better than static numbers
  - - Created adoption-metrics-tracking.md as the single source of truth for distribution status, baseline, and ROI analysis rather than scattering across multiple files


### 2026-04-15 — [task-20260415-120230-diagnose-and-optimize-github-visitor-to-star-conve] (#204 kai-linux/agent-os)
Diagnosed the GitHub visitor-to-star conversion funnel using 14-day traffic data. The primary finding is that the funnel is empty at the top: only 4 unique visitors in 14 days with zero external referrers. All README/demo/quickstart optimization work is wasted because nobody is arriving. The 591 "unique cloners" are almost entirely automated worktree operations, not real users. Both stars were acquired on March 17-18 and none since. Documented top 3 friction barriers ranked by impact, proposed specific fixes for each, and implemented two zero-cost changes (star CTA in README, diagnostic report).

**Files:** `- docs/conversion-funnel-analysis.md`, `- README.md`, `- .agent_result.md`

**Decisions:**
  - - Ranked "zero distribution" as barrier #1 because all downstream conversion optimization is moot with 4 unique visitors in 14 days
  - - Identified clone volume as self-traffic by correlating clone spikes with task dispatch dates (e.g., 645 clones on Apr 9 = high task execution day)
  - - Chose minimal README change (star CTA) over larger restructuring because the bottleneck is traffic, not README quality
  - - Confirmed Discussion pinning is genuinely UI-only (no GraphQL mutation exists) — listed as manual step
  - - Did not attempt dev.to/HN/Reddit posting because DEV_API_KEY is not set and the other platforms have no API for posting


### 2026-04-15 — [task-20260415-120149-create-contributing-md-with-contributor-guidelines] (#207 kai-linux/agent-os)
Created CONTRIBUTING.md with contributor guidelines covering setup, commit conventions (Conventional Commits), PR expectations, code review process, issue filing format, code style, and project layout. Kept concise and example-driven for external Python developers familiar with git.

**Files:** `- CONTRIBUTING.md`, `- .agent_result.md`

**Decisions:**
  - - Used Conventional Commits as the documented convention since existing commits follow that pattern (feat:, fix:, chore:, docs:)
  - - Kept the guide under 100 lines of content to stay concise per constraints
  - - Referenced the existing .github/ISSUE_TEMPLATE/agent-task.md rather than duplicating its content
  - - Documented squash-merge as the merge strategy since PR titles follow conventional commit format


### 2026-04-14 — [task-20260414-220519-validate-codex-agent-stabilization-fix-and-prevent] (#198 kai-linux/agent-os)
Validated the codex agent runtime stabilization fix (task-20260409-210219). Post-fix codex success rate is 100% (2/2), though sample size is small. The 14-day rate of 75% (6/8) includes 2 pre-fix debugging failures — the fix correctly scopes scoring by task type so these no longer affect routing. Added 3 regression tests validating task-type-scoped gating, small-sample protection, and degradation finding scoping. Added continuous "Codex Runtime Stability" monitoring section to health_gate_report.py with 85% warning threshold. Documented full RCA and validation findings in CODEBASE.md. The >85% target is met post-fix but needs continued monitoring due to small sample size.

**Files:** `- tests/test_agent_scorer.py`, `- tests/test_health_gate_report.py`, `- orchestrator/health_gate_report.py`, `- CODEBASE.md`

**Decisions:**
  - - Post-fix codex is 2/2 (100%) but sample is n=2, so statistical confidence is limited — documented this and added continuous monitoring rather than claiming definitive >85%
  - - The 14d rate of 75% includes 2 pre-fix failures (Apr 1 debugging tasks with missing_context); excluding pre-fix data yields 100%
  - - Root cause confirmed: blended scoring across task types artificially depressed codex rate; fix correctly scopes by task type
  - - Added monitoring section to existing health_gate_report.py rather than creating new infrastructure
  - - Regression tests focus on the three key fix mechanisms: task-type scoping, small-sample protection, degradation finding scoping


### 2026-04-14 — [task-20260414-220418-reduce-missing-context-blockers-through-task-intak] (#197 kai-linux/agent-os)
Added a non-blocking task validation gate to the dispatcher that checks for required context fields (issue link, repo, task description, acceptance criteria) at dispatch time. Missing fields emit warnings to stdout and are recorded in task frontmatter (context_complete, context_missing). A separate telemetry log (runtime/metrics/context_completeness.jsonl) tracks completeness per task, segmented by agent and task type, enabling monitoring of missing_context trends. Validation is entirely non-blocking — tasks dispatch regardless of warnings.

**Files:** `- orchestrator/github_dispatcher.py`, `- tests/test_github_dispatcher.py`

**Decisions:**
  - - Placed validation in build_mailbox_task() rather than _dispatch_item() so it runs for both direct dispatches and decomposed child dispatches
  - - Used a separate JSONL telemetry file (context_completeness.jsonl) rather than extending agent_stats.jsonl, since context validation happens at dispatch time (not task completion) and has different schema
  - - Stored validation results in task frontmatter (context_complete, context_missing) so downstream queue/review tooling can consume the data
  - - Kept validation purely field-presence based (no heuristics) per constraints
  - - Used fire-and-forget try/except for telemetry writes so logging failures never block dispatch


### 2026-04-14 — [task-20260414-220319-publish-operational-reliability-dashboard-to-drive] (#196 kai-linux/agent-os)
Populated the reliability dashboard with live operational metrics (91% success rate, 0.1h mean completion, 0% escalation rate over 14-day window) and added GitHub stars/forks display. Added GitHub metrics fetching to public_dashboard.py via gh API, updated the run script to auto-commit dashboard refreshes, and installed a daily cron at 05:00 UTC for auto-update. The dashboard now renders with real data on GitHub at docs/reliability/README.md.

**Files:** `- orchestrator/public_dashboard.py`, `- bin/run_public_dashboard.sh`, `- docs/reliability/README.md`, `- docs/reliability/index.html`, `- docs/reliability/metrics.json`

**Decisions:**
  - - Used gh api repos/{slug} for GitHub metrics instead of reading evidence JSONL, keeping the dashboard self-contained with fresh data on each run
  - - Auto-detect repo slug from git remote origin URL rather than hardcoding
  - - Installed cron at 05:00 UTC daily (before the 06:00 product inspector and 08:00 daily digest)
  - - Updated run script to auto-commit+push dashboard changes so GitHub always shows current metrics
  - - Kept existing dashboard structure (3 output files) and only added GitHub metrics as new rows/cards


### 2026-04-14 — [task-20260414-220222-embed-agent-performance-data-and-success-stories-i] (#194 kai-linux/agent-os)
Embedded agent performance metrics (91% 14-day success rate, 3.2 min median completion, 18% escalation rate) directly in the README above the fold, and updated the Built with Agent OS section with current numbers (146 tasks, 103 issues closed, 79 PRs merged, 338 commits). Both sections link to the reliability dashboard and case study for detail.

**Files:** `- README.md`

**Decisions:**
  - - Placed performance metrics section immediately after the demo and before the Goal section for visibility within 10 seconds of scroll
  - - Used a compact 4-column table for the key metrics (success rate, median completion, escalation rate, total tasks) to keep it scannable
  - - Used 14-day rolling success rate (91%) as the headline number since it reflects current system health better than the all-time 62% which includes early bootstrapping failures
  - - Updated Built with Agent OS section to show both 14-day and overall success rates for transparency
  - - Removed redundant prose lines in Built with Agent OS section to reduce clutter while preserving all links


### 2026-04-14 — [task-20260414-220123-boost-github-discoverability-through-search-and-tr] (#157 kai-linux/agent-os)
Optimized GitHub repository metadata for discoverability: updated description with key search terms (autonomous agent, multi-agent system, task routing), expanded topics from 16 to 20 (adding agent-os, agent-framework, multi-agent-system, llm-agents, self-healing, ci-cd), set homepage to the case study discussion, and added traffic metrics capture to the existing evidence export script for weekly tracking. Baseline captured: 2 stars, 0 forks, 63 views (4 unique), 2109 clones (587 unique) over the 14-day window.

**Files:** `- bin/export_github_evidence.sh`

**Decisions:**
  - - Used gh api repos/:owner/:repo/topics PUT to set topics directly via API rather than requiring manual GitHub UI changes
  - - Used gh api repos/:owner/:repo PATCH to update description and homepage via API
  - - Extended existing evidence export script rather than creating a new script, keeping the cron infrastructure unchanged
  - - Chose 20 topics (GitHub max) prioritizing search terms from the issue: autonomous-agents, agent-framework, multi-agent-system, agent-os, llm-agents
  - - Dropped kanban and orchestrations topics (low search value) to make room for higher-value terms


### 2026-04-12 — [task-20260412-123919-fix-readme-credibility-signals-make-proof-links-fu] (#188 kai-linux/agent-os)
Fixed README credibility signals by making the "Public proof" line prominent with bold formatting and consolidating all three proof links (reliability dashboard, case study, GitHub discussion) into a single scannable line near the top. Fixed arrow inconsistency in "Built with Agent OS" section and added the community discussion link there for redundancy.

**Files:** `- README.md`

**Decisions:**
  - - Used · -separated inline links instead of a table or bullet list for the top proof line, keeping it scannable and compact above the fold
  - - Kept the existing "Built with Agent OS" section structure and added links there for redundancy rather than removing the section's own proof links
  - - Did not change the reliability dashboard content (all n/a) since the task scope is about making links functional and prominent, not populating metrics data


### 2026-04-12 — [task-20260412-123819-complete-case-study-distribution-and-measure-adopt] (#187 kai-linux/agent-os)
Completed the case study distribution cycle by refreshing all promotion content with current metrics (327 commits, 75 PRs merged, 100 issues closed, 60.8% success rate over 27 days), capturing an adoption metrics baseline (2 stars, 0 forks), creating an automated distribution script (bin/publish_case_study.sh) that posts to dev.to when DEV_API_KEY is set and logs distribution events, updating the README with measured adoption data including star/fork counts, and adding an adoption metrics section to the case study for sprint retrospective evidence.

**Files:** `- bin/publish_case_study.sh`, `- docs/case-study-agent-os.md`, `- docs/promotion/devto-article.md`, `- docs/promotion/hn-submission.md`, `- docs/promotion/README.md`, `- README.md`, `- .agent_result.md`

**Decisions:**
  - - Created bin/publish_case_study.sh as the single entry point for distribution + metrics capture, following the existing bin/ script pattern
  - - dev.to publishing requires DEV_API_KEY env var — script skips gracefully when not set and logs the skip to distribution_log.jsonl
  - - HN submission remains manual (no write API exists) — documented in script output and promotion/README.md
  - - Refreshed all promotion content with live metrics from GitHub API and agent_stats.jsonl rather than using stale 23-day numbers
  - - Added measured adoption signals (stars, forks) directly to README metrics table so they are visible as public credibility signals per NORTH_STAR.md guidance


### 2026-04-12 — [task-20260412-123718-create-deployment-guide-for-solo-builder-adoption] (#185 kai-linux/agent-os)
Created a comprehensive deployment guide (docs/deployment-guide.md, 476 lines) covering prerequisites, installation, configuration examples for Python/Node.js/monorepo setups, GitHub Project board setup, manual first-task walkthrough, cron configuration for production, upgrade path to full automation, Telegram notifications, and a troubleshooting section for common errors. Linked the guide from README.md documentation table.

**Files:** `- docs/deployment-guide.md`, `- README.md`

**Decisions:**
  - - Used dispatcher_only as the recommended starting mode so new users can verify the flow before enabling full automation
  - - Kept config examples minimal (only essential fields) and referenced example.config.yaml for advanced options
  - - Structured the guide as a progressive walkthrough: manual first → cron → full automation
  - - Put troubleshooting at the end with the 7 most common failure modes based on codebase history (auth failures, blocked tasks, worktree errors, cron issues)
  - - Added the guide as the first row in the README documentation table (bolded) for maximum discoverability


### 2026-04-12 — [task-20260412-123618-add-comprehensive-quickstart-section-to-readme] (#184 kai-linux/agent-os)
Replaced the existing "Try It in 5 Minutes" section in README.md with a comprehensive "Get Started in 5 Minutes" quickstart offering two paths: a 2-minute sandbox demo (referencing demo.sh) and a 5-minute production setup with 6 copy-paste steps covering installation, GitHub auth, configuration, task creation, dispatch, and viewing results. The section is ~105 lines, positioned in the top third of the README, and works on macOS and Linux.

**Files:** `- README.md`, `- .agent_result.md`

**Decisions:**
  - - Replaced the old "Try It in 5 Minutes" section in-place rather than adding a new section, keeping the same prominent position and avoiding duplicate content
  - - Split into Option A (sandbox, 2 min) and Option B (production, 5 min) to serve both tire-kickers and serious adopters
  - - Showed minimal config.yaml with only the 4 essential fields (root_dir, worktrees_dir, allowed_repos, default_allow_push) to minimize friction
  - - Included the issue body template (Goal/Success Criteria/Constraints) since that's the dispatcher's expected format
  - - Put cron setup behind a details/summary collapse since it's optional and the manual dispatch commands already demonstrate the full loop


### 2026-04-12 — [task-20260412-123519-configure-external-outcome-metrics-for-adoption-pr] (#183 kai-linux/agent-os)
Implemented automated baseline capture at PR merge time (GitHub stars/forks via gh API + operational metrics from agent_stats.jsonl), wired all 5 objective metrics with outcome_check blocks including 7-day and 14-day measurement windows, enhanced the outcome snapshot prompt with baseline data and prior-trend analysis to avoid false causality, enabled outcome_attribution by default, and ensured objective-derived checks flow through dispatch so adoption PRs are instrumented automatically.

**Files:** `- orchestrator/outcome_attribution.py`, `- orchestrator/pr_monitor.py`, `- orchestrator/strategic_planner.py`, `- objectives/agent-os.yaml`, `- example.config.yaml`, `- tests/test_strategic_planner.py`

**Decisions:**
  - - Pointed outcome checks to github_metrics_history.jsonl (JSONL trend log) instead of *_latest.yaml (single-point snapshot) so the LLM evaluator can see before/after data points across the measurement window
  - - Captured baseline metrics in the attribution record at merge time rather than adding a separate baseline store, keeping the data co-located with the event it describes
  - - Added 14-day variants as zero-weight metrics (github_stars_14d, github_forks_14d) so they generate snapshots without affecting the objective score — they serve as validation checks
  - - Used lazy import for objectives in get_repo_outcome_check_ids to avoid circular imports
  - - Added prior-trend analysis instructions to the snapshot prompt to mitigate false causality attribution


### 2026-04-10 — [task-20260410-190217-add-error-handling-and-rate-limiting-to-telegram-i] (#38 kai-linux/agent-os)
deepseek failed before producing a valid result file. Runner exited with code 1 while executing `/home/kai/agent-os/bin/agent_runner.sh deepseek /srv/worktrees/agent-os/task-20260410-190217-add-error-handling-and-rate-limiting-to-telegram-i /home/kai/agent-os/runtime/tmp/task-20260410-190217-add-error-handling-and-rate-limiting-to-telegram-i.txt`. Classified as: authentication failure. Orchestrator rescued and pushed the worktree changes.

**Files:** `- Unknown / inspect worktree`

**Decisions:**
  - - Treat runner failure as model-level failure and continue fallback chain if possible.
  - - Queue performed git rescue after the agent left valid changes behind.


### 2026-04-10 — [task-20260410-190122-validate-codex-agent-fix-and-add-regression-monito] (#182 kai-linux/agent-os)
deepseek failed before producing a valid result file. Runner exited with code 1 while executing `/home/kai/agent-os/bin/agent_runner.sh deepseek /srv/worktrees/agent-os/task-20260410-190122-validate-codex-agent-fix-and-add-regression-monito /home/kai/agent-os/runtime/tmp/task-20260410-190122-validate-codex-agent-fix-and-add-regression-monito.txt`. Classified as: authentication failure. Orchestrator rescued and pushed the worktree changes.

**Files:** `- Unknown / inspect worktree`

**Decisions:**
  - - Treat runner failure as model-level failure and continue fallback chain if possible.
  - - Queue performed git rescue after the agent left valid changes behind.


### 2026-04-09 — [task-20260409-210702-set-dev-api-key-environment-variable-to-enable-aut] (#172 kai-linux/agent-os)
codex failed before producing a valid result file. Runner exited with code 1 while executing `/home/kai/agent-os/bin/agent_runner.sh codex /srv/worktrees/agent-os/task-20260409-210702-set-dev-api-key-environment-variable-to-enable-aut /home/kai/agent-os/runtime/tmp/task-20260409-210702-set-dev-api-key-environment-variable-to-enable-aut.txt`. Classified as: usage limit / rate limit. Orchestrator rescued and pushed the worktree changes.

**Files:** `- Unknown / inspect worktree`

**Decisions:**
  - - Treat runner failure as model-level failure and continue fallback chain if possible.
  - - Queue performed git rescue after the agent left valid changes behind.


### 2026-04-09 — [task-20260409-210522-validate-missing-context-blocker-reduction-and-con] (#175 kai-linux/agent-os)
Validated that the structured dispatch context fix (task-20260409-070520, issue #159) reduced missing_context blockers from 7 instances (pre-fix 14-day window) to 0 instances post-fix. Implemented forward-looking Telegram regression alerting in log_analyzer.py that fires when missing_context exceeds 5 in a rolling 24h window (excluding historical backlog data). Added blocker code distribution with regression test guidance to PRODUCTION_FEEDBACK.md generation, and documented the alert threshold and RCA runbook in example.config.yaml.

**Files:** `- orchestrator/log_analyzer.py`, `- orchestrator/agent_scorer.py`, `- example.config.yaml`, `- tests/test_log_analyzer.py`, `- .agent_result.md`

**Decisions:**
  - - Used log_analyzer.py as the alert host since it already runs on a weekly cron and processes agent_stats.jsonl with Telegram sending capability
  - - fix_timestamp parameter defaults to the actual fix deployment time (2026-04-09T07:53) to prevent alerting on historical data
  - - Threshold of 5 (> 5 fires alert) chosen as the regression threshold since baseline was 7 and target is < 3
  - - RCA runbook embedded directly in the Telegram alert message for immediate actionability
  - - Enhanced PRODUCTION_FEEDBACK.md generation rather than manually editing the file, so blocker distribution stays current across future regenerations


### 2026-04-09 — [task-20260409-210416-attach-prompt-snapshot-references-to-blocked-task-] (#70 kai-linux/agent-os)
Attached prompt snapshot references to all blocked task escalation surfaces (dispatcher escalation notes, GitHub issue comments, Telegram messages, Telegram action payloads, and queue escalation notes) by reading the existing `prompt_snapshot_path` from task frontmatter metadata.

**Files:** `- orchestrator/github_dispatcher.py`, `- orchestrator/queue.py`, `- tests/test_github_dispatcher.py`, `- tests/test_queue.py`, `- CODEBASE.md`

**Decisions:**
  - - Reused existing `prompt_snapshot_path` from task frontmatter instead of adding new persistence or lookup
  - - Added `_resolve_prompt_snapshot_path()` helper to normalize missing paths to "none" for clean display
  - - Embedded the reference in all five escalation surfaces for full traceability from any escalation output
  - - Kept the diff minimal by only adding fields to existing data structures
Attached prompt snapshot references to blocked task escalation payloads so operators can trace a blocked task back to the exact prompt context without searching logs. The `prompt_snapshot_path` from task metadata is now embedded in dispatcher escalation notes, GitHub issue comments, Telegram messages, Telegram action payloads, and queue-side escalation notes.

**Files:** `- orchestrator/github_dispatcher.py`, `- orchestrator/queue.py`, `- tests/test_github_dispatcher.py`, `- tests/test_queue.py`

**Decisions:**
  - Reused existing `prompt_snapshot_path` from task frontmatter metadata instead of adding new persistence or lookup logic
  - Added a `_resolve_prompt_snapshot_path()` helper in github_dispatcher.py to normalize absent paths to "none"
  - Embedded the reference in all five escalation surfaces for full traceability


### 2026-04-09 — [task-20260409-210321-create-public-reliability-metrics-dashboard-to-sup] (#176 kai-linux/agent-os)
deepseek failed before producing a valid result file. Runner exited with code 1 while executing `/home/kai/agent-os/bin/agent_runner.sh deepseek /srv/worktrees/agent-os/task-20260409-210321-create-public-reliability-metrics-dashboard-to-sup /home/kai/agent-os/runtime/tmp/task-20260409-210321-create-public-reliability-metrics-dashboard-to-sup.txt`. Classified as: authentication failure. Orchestrator rescued and pushed the worktree changes.

**Files:** `- Unknown / inspect worktree`

**Decisions:**
  - - Treat runner failure as model-level failure and continue fallback chain if possible.
  - - Queue performed git rescue after the agent left valid changes behind.


### 2026-04-09 — [task-20260409-210219-investigate-and-fix-codex-agent-runtime-degradatio] (#173 kai-linux/agent-os)
Root cause was metric and routing instability, not a new codex CLI regression: codex was being scored as one blended bucket across mixed task types, so debugging-path decisions were polluted by unrelated implementation outcomes. I fixed the scorer and health gates to use task-type-aware success rates with an overall fallback for small samples, and added reporting/tests for the debugging slice.

**Files:** `- .agent_result.md`, `- orchestrator/agent_scorer.py`, `- orchestrator/github_dispatcher.py`, `- orchestrator/health_gate_report.py`, `- orchestrator/queue.py`, `- tests/test_agent_scorer.py`, `- tests/test_github_dispatcher.py`, `- tests/test_health_gate_report.py`

**Decisions:**
  - - Treated the degradation as a task-slice scoring problem because the issue explicitly targets codex on debugging tasks and the existing scorer blended heterogeneous workloads.
  - - Kept the diff bounded by reusing existing `agent_stats.jsonl` telemetry instead of adding new persistence or a separate codex-only metric store.
  - - Used task-type-specific rates only when there are at least 3 records in the current window, falling back to overall agent rates to avoid unstable routing on tiny samples.


### 2026-04-09 — [task-20260409-070520-reduce-missing-context-task-blockers-through-enhan] (#159 kai-linux/agent-os)
Enhanced the task-dispatch context template with three new structured context sections — recent git state (10 commits on base branch), objective alignment (tracked metrics with weights/directions), and sprint directives — injected into write_prompt() as a "Dispatch Context (structured)" block. These address the root causes of 7 missing_context blockers in the last 14 days (all debugging tasks where agents lacked visibility into recent repo changes, objective metrics, and sprint priorities).

**Files:** `- orchestrator/repo_context.py`, `- orchestrator/queue.py`, `- tests/test_queue.py`

**Decisions:**
  - - Added context in write_prompt() rather than build_execution_context() to access task metadata (base_branch, github_repo) without changing the shared function signature
  - - Used subprocess with 5s timeout for git log to keep overhead well under 1s constraint
  - - Lazy-loaded objectives via try/except import to avoid circular imports
  - - Sprint directives use existing read_sprint_directives() which was already available but not wired into worker prompts
  - - Enhanced context only injected when worktree exists (not for prompt-less invocations)


### 2026-04-09 — [task-20260409-070419-validate-and-monitor-adaptive-agent-health-gate-im] (#162 kai-linux/agent-os)
Added health gate monitoring infrastructure: JSONL audit trail for gate decisions (health_gate_decisions.jsonl), a weekly report generator (orchestrator/health_gate_report.py) that produces a markdown validation report with baseline metrics, window metrics, gate decision analysis, blocker code trends, validation status, and false positive detection. Baseline captured: claude 100%, codex 60.3%, deepseek 46.7%, overall 68.8%. Last 7 days shows 100% success rate with only claude dispatched and zero fallback_exhausted events, confirming the gate is effective.

**Files:** `- orchestrator/agent_scorer.py`, `- orchestrator/github_dispatcher.py`, `- orchestrator/queue.py`, `- orchestrator/health_gate_report.py`, `- bin/run_health_gate_report.sh`, `- tests/test_health_gate_report.py`, `- .gitignore`

**Decisions:**
  - - Used JSONL format for gate decision audit log consistent with existing agent_stats.jsonl pattern
  - - Gate decisions only logged when agents are actually skipped (no-op invocations are silent)
  - - Report generator reads both agent_stats.jsonl and gate decisions for cross-referencing
  - - Weekly cron schedule aligns with existing log analyzer cadence
  - - No threshold changes made per constraint (need 3+ days of decision data first)


### 2026-04-09 — [task-20260409-070319-publish-github-discussions-case-study-autonomous-m] (#161 kai-linux/agent-os)
Published a detailed case study as GitHub Discussion #167 (https://github.com/kai-linux/agent-os/discussions/167) in the "Show and tell" category. The discussion showcases the PR-98 cascading failure recovery workflow with concrete metrics (288 commits, 59+ PRs merged, 79+ issues closed, ~8 min median PR cycle time), before/after comparison (manual vs Agent OS approach), honest failure reporting, sample PR cycle times with file/line counts, and links to all verifiable public GitHub data. Enabled GitHub Discussions on the repo as a prerequisite.

**Files:** `- .agent_result.md`

**Decisions:**
  - - Used "Show and tell" category instead of "Announcements" since the case study is a showcase, not a release announcement
  - - Focused the deep dive on PR-98 cascading failure recovery as the most compelling multi-agent workflow story (shows both failure and recovery)
  - - Included honest failure metrics (55.7% success rate, cascade waste) to build trust per task constraints
  - - Kept under 2000 words (~1500 words in the discussion body) for scannability
  - - Enabled GitHub Discussions as a prerequisite since it was disabled


### 2026-04-09 — [task-20260409-070215-fix-readme-rendering-and-complete-truncated-sectio] (#156 kai-linux/agent-os)
Fixed README rendering truncation by breaking the 209-character line containing "Indefinitely" into shorter lines that won't get clipped in preview contexts. Added a "Get Involved" CTA section with try-it, contribute, and contact calls-to-action. All sections render completely with no truncation.

**Files:** `- README.md`

**Decisions:**
  - - Broke the 209-char paragraph into 3 lines rather than rewriting it, preserving the existing copy
  - - Added the CTA section at the very end after Documentation, keeping it as a clean closing section
  - - Linked "Questions?" to the issues page since the repo doesn't have GitHub Discussions enabled


### 2026-04-09 — [task-20260409-070128-harden-product-inspection-md-per-observation-prove] (#137 kai-linux/agent-os)
Hardened the product inspection pipeline with per-observation provenance (source URL, fetch timestamp, HTTP status, response size, extraction confidence), cadence-aligned staleness detection, explicit coverage boundary framing, and transient-failure detection with consecutive failure tracking. Non-200 responses are distinctly tagged and targets with 3+ consecutive failures are marked low_confidence. The planner and groomer prompts now include rules to correctly interpret coverage boundaries and down-weight low-confidence signals.

**Files:** `- orchestrator/product_inspector.py`, `- orchestrator/strategic_planner.py`, `- orchestrator/backlog_groomer.py`, `- tests/test_product_inspector.py`, `- .gitignore`

**Decisions:**
  - - Used curl -w "\n%{http_code}" to extract HTTP status from fetches rather than adding a separate HEAD request, keeping the single-fetch-per-target model
  - - Stored failure history in .product_inspection_failures.json in the managed repo root (gitignored) rather than runtime/metrics/ since it's per-repo state tied to inspection targets
  - - Set CONSECUTIVE_FAILURE_THRESHOLD=3 as a reasonable default for transient vs persistent failure classification
  - - Coverage boundary always lists "All authenticated flows" and "JavaScript-rendered content" as uninspected since those are fundamental limitations of the text-only fetch approach
  - - cadence_hours parameter defaults to 0 (use configured max_age_hours) for backward compatibility; only the strategic planner passes it


### 2026-04-08 — [task-20260408-150519-add-github-adoption-metrics-to-production-feedback] (#150 kai-linux/agent-os)
Added GitHub adoption metrics (stars, forks, 14-day growth delta, and trend status) to PRODUCTION_FEEDBACK.md by adding three helper functions to strategic_planner.py and inserting an "External Adoption Signals" section into the substrate production feedback generation. Metrics are fetched live via the public GitHub API (gh cli) and growth trends are computed from the existing evidence history JSONL written by bin/export_github_evidence.sh.

**Files:** `- orchestrator/strategic_planner.py`

**Decisions:**
  - - Reused the existing evidence history JSONL (github_metrics_history.jsonl) written by bin/export_github_evidence.sh rather than adding a new data store, keeping the adoption metrics pipeline consistent
  - - Used gh api directly for live star/fork counts (same pattern as backlog_groomer._fetch_github_stars_forks) rather than reading only from evidence snapshots, so feedback is fresh even if the evidence exporter hasn't run recently
  - - Added the adoption section as the last entry in _build_substrate_production_feedback_sections() so it appears after operational metrics, consistent with adoption being a lagging indicator per NORTH_STAR.md
  - - Growth trend classification uses simple categories (growing/stalled/regressed/insufficient data) to report data without speculating on causation per task constraints


### 2026-04-08 — [task-20260408-150416-implement-adaptive-agent-health-checks-in-task-dis] (#149 kai-linux/agent-os)
Added an adaptive 7-day health gate with a 25% success rate threshold to both the dispatcher and queue agent chain resolution. Agents with <25% success rate over the last 7 days (e.g., deepseek at 0%) are automatically skipped before dispatch, with the skip reason logged. The gate uses the same agent_stats.jsonl metrics that feed PRODUCTION_FEEDBACK.md, and agents automatically recover when their metrics improve above the threshold.

**Files:** `- orchestrator/agent_scorer.py`, `- orchestrator/github_dispatcher.py`, `- orchestrator/queue.py`, `- tests/test_queue.py`, `- tests/test_github_dispatcher.py`

**Decisions:**
  - - Reused existing filter_healthy_agents() with wider window (7d) and lower threshold (25%) rather than parsing PRODUCTION_FEEDBACK.md markdown, because both consume the same agent_stats.jsonl data source and the function approach is more reliable than regex parsing
  - - Applied the adaptive gate before the existing 24h/80% gate so severely degraded agents are removed first, then the short-window gate applies to the remaining candidates
  - - Added the gate to both dispatcher (build_mailbox_task path) and queue (get_agent_chain path) for consistent behavior at dispatch time and execution time


### 2026-04-08 — [task-20260408-150317-rca-and-fix-for-pr-98-cascading-ci-failure-pattern] (#148 kai-linux/agent-os)
Root cause identified and fixed for the PR-98 cascading CI failure pattern. The CI completion verification gate (`verify_pr_ci_debug_completion`) was extracting failed job names from markdown prose in issue bodies, which got lost when follow-up tasks reformatted the body text. This caused `missing_failed_job_context` downgrades on successful fixes, spawning 8+ cascading debug tasks. Fix: persist `failed_checks` as structured frontmatter metadata at dispatch time, read it in the verification gate before falling back to body parsing, and propagate it through follow-up task creation.

**Files:** `- orchestrator/queue.py`, `- orchestrator/github_dispatcher.py`, `- tests/test_queue.py`, `- CODEBASE.md`

**Decisions:**
  - - Persisted failed_checks as structured task frontmatter rather than relying on markdown prose parsing, because markdown survives zero reformatting guarantees across follow-up handoffs
  - - Used meta-first fallback pattern (read meta["failed_checks"], fall back to body parsing) for backward compatibility with existing tasks that lack the field
  - - Propagated failed_checks in follow-up task creation to prevent metadata loss across the full debug task chain
  - - Extracted check names from issue body at dispatch time using the existing _extract_ci_checks_from_body helper


### 2026-04-08 — [task-20260408-150222-publish-first-external-adoption-proof-managed-repo] (#147 kai-linux/agent-os)
Created a public case study (docs/case-study-agent-os.md) documenting agent-os managing its own repository over 23 days with auditable before/after metrics (79 issues closed, 59 PRs merged, 275 commits, 55.7% first-attempt success rate). Added a "Built with agent-os" section to README with a metrics table and link to the case study. PR #152 opened.

**Files:** `- docs/case-study-agent-os.md`, `- README.md`

**Decisions:**
  - - Used agent-os itself as the managed repo case study since it is the primary managed repository with 23 days of operational data
  - - Included an ASCII bar chart rather than an image to keep the case study self-contained and renderable on GitHub without external dependencies
  - - All metrics sourced from public GitHub data (gh issue list, gh pr list, git log) and runtime/metrics/agent_stats.jsonl for task success rate
  - - Kept the README addition concise (metrics table + 3 sentences + link) to preserve the scannable 1-page pitch format


### 2026-04-08 — [task-20260408-150119-improve-github-discoverability-with-trending-signa] (#146 kai-linux/agent-os)
Added CI status, forks, issues, and license badges to README and set missing GitHub topics (multi-agent, automation) via the gh API. All five requested topics are now present on the repo. PR #151 opened.

**Files:** `- README.md`

**Decisions:**
  - - Added badges in a single row above the existing stars badge line rather than a separate section, keeping the minimal-diff constraint
  - - Used shields.io badges consistent with the existing stars badge style
  - - Added social-style badges for stars/forks and standard badges for CI/issues/license for visual variety


### 2026-04-07 — [task-20260407-100426-persist-publish-block-reasons-on-git-push-readines] (#88 kai-linux/agent-os)
Added `push_not_ready` as a first-class blocker code and wired the dispatcher's push-readiness skip path to persist a structured unblock-notes artifact to `runtime/unblock_notes/`, making publish-block reasons queryable by backlog grooming and retry logic.

**Files:** `- orchestrator/queue.py`, `- orchestrator/github_dispatcher.py`, `- tests/test_github_dispatcher.py`

**Decisions:**
  - - Used synthetic task ID `dispatch-{owner}-{repo}-{issue_number}` for the artifact since no mailbox task_id exists at dispatch time
  - - Wrapped artifact write in try/except so failures don't break dispatch flow
  - - Kept the existing GitHub issue comment payload unchanged; the artifact is additive


### 2026-04-07 — [task-20260407-100229-sprint-plan-skip-auto-skip-should-write-a-signal-t] (#136 kai-linux/agent-os)
Implemented skip signal persistence so sprint plan skips (explicit and auto-skip) are recorded to a JSONL store at runtime/metrics/plan_skip_signals.jsonl. The planner reads recent skip signals on next cycle, injects skip history into the LLM prompt to avoid identical compositions, and includes a diff line in the Telegram plan message (e.g., "No change from previous plan" or "Reordered: #96↔#52"). The groomer reads skip signals to apply cadence backoff (halves issue generation after 2+ auto-skips) and anti-repeat penalties (injects explicitly-skipped issue context into the LLM prompt). Penalties decay with a 7-day half-life so issues can resurface.

**Files:** `- orchestrator/skip_signals.py`, `- orchestrator/strategic_planner.py`, `- orchestrator/backlog_groomer.py`, `- orchestrator/paths.py`, `- tests/test_strategic_planner.py`, `- tests/test_backlog_groomer.py`

**Decisions:**
  - - Extracted skip signal logic to orchestrator/skip_signals.py to avoid circular import between strategic_planner and backlog_groomer
  - - Used JSONL at runtime/metrics/plan_skip_signals.jsonl consistent with existing metrics pattern (agent_stats.jsonl, review_signals.jsonl)
  - - Fingerprint is sorted comma-joined issue numbers for stable deduplication
  - - Explicit skip penalty weight (3.0) is 3x auto-skip weight (1.0) to distinguish signal strength
  - - Penalty half-life of 7 days matches sprint cadence so issues can resurface after one cycle


### 2026-04-07 — [task-20260407-100318-consume-pr-review-signals-for-task-routing-and-fol] (#96 kai-linux/agent-os)
Integrated PR review signal extraction into pr_monitor's post-merge flow. When a PR merges, risk assessment signals (coverage gap, risk level, diff size) are recorded to a JSONL log at runtime/metrics/review_signals.jsonl. A query layer identifies flagged signals (coverage gaps, high-risk merges) and a bounded follow-up generator creates deduped GitHub issues for PRs with quality flags, capped at 3 per sprint window.

**Files:** `- orchestrator/review_signals.py`, `- orchestrator/pr_monitor.py`, `- tests/test_review_signals.py`

**Decisions:**
  - - Used the same JSONL persistence pattern as outcome_attribution for review signals, storing to runtime/metrics/review_signals.jsonl
  - - Started with 2 high-confidence signals: coverage_gap (source changes without tests) and high_risk (from existing risk assessment)
  - - Bounded follow-ups to MAX_FOLLOWUPS_PER_SPRINT=3 and deduped by exact title match against open issues
  - - Re-assessed PR risk at merge time rather than caching RiskAssessment objects in state, since diff stat calls are cheap
  - - Kept follow-up generation in the monitor_prs cycle rather than a separate job, since it runs alongside existing PR processing


### 2026-04-07 — [task-20260407-100119-require-unblock-notes-for-partial-and-blocked-task] (#52 kai-linux/agent-os)
Implemented structured unblock notes enforcement for partial and blocked task outcomes. The UNBLOCK_NOTES section (with blocking_cause and next_action fields) is now required in .agent_result.md for non-complete outcomes, validated during parsing, written as a machine-readable YAML artifact to runtime/unblock_notes/{task_id}.yaml, and carried through to follow-up tasks, escalation notes, and GitHub sync comments.

**Files:** `- orchestrator/queue.py`, `- orchestrator/github_sync.py`, `- tests/test_queue.py`

**Decisions:**
  - - Used bullet-style format (- blocking_cause: ..., - next_action: ...) for UNBLOCK_NOTES in .agent_result.md to match existing section conventions
  - - Wrote machine-readable artifact as YAML to runtime/unblock_notes/ directory, consistent with other runtime artifacts
  - - Made all system-generated blocked/partial results include unblock_notes so they pass their own validation when re-parsed
  - - Kept validation in parse_agent_result() alongside existing blocker_code validation for consistency


### 2026-04-05 — [task-20260405-090117-add-domain-specific-evaluation-rubrics-for-plannin] (#42 kai-linux/agent-os)
Added domain-specific evaluation rubrics so repos can declare what "good" looks like via `RUBRIC.md`. The rubric is read by `repo_context.read_evaluation_rubric()` and injected into the strategic planner prompt, backlog groomer prompt, and worker execution context (for architecture/research tasks). When present, planners and groomers use the rubric's quality dimensions and skill dimensions to evaluate and shape work. When absent, a fallback message is shown and behavior is unchanged.

**Files:** `- orchestrator/repo_context.py`, `- orchestrator/strategic_planner.py`, `- orchestrator/backlog_groomer.py`, `- RUBRIC.md`, `- PLANNING_PRINCIPLES.md`, `- tests/test_queue.py`, `- tests/test_strategic_planner.py`

**Decisions:**
  - - Used a convention file (`RUBRIC.md`) in each managed repo rather than config.yaml entries, keeping rubrics inspectable and editable in-repo
  - - Returns empty string (not a fallback message) from `read_evaluation_rubric()` when the file is absent, so the execution context omits the section entirely for repos without rubrics
  - - Injected rubric as a new prompt section rather than modifying existing prompt sections, keeping the diff minimal and each context layer independent
  - - Documented rubric usage in PLANNING_PRINCIPLES.md so the planner's stable rubric references domain evaluation as a first-class input

### 2026-04-04 — [task-20260404-130416-teach-the-backlog-groomer-to-generate-adoption-and] (#127 kai-linux/agent-os)
Updated the backlog groomer to gather and inject adoption/credibility signals (GitHub stars/forks, README structure assessment, quickstart friction level, demo availability) into the LLM prompt, so the groomer now produces a balanced mix of infrastructure and adoption-focused issues. The prompt already had balance rules requiring at least 1 in 5 issues to target adoption; the new concrete signals give the LLM the data it needs to actually generate actionable adoption issues.

**Files:** `- orchestrator/backlog_groomer.py`, `- tests/test_backlog_groomer.py`

**Decisions:**
  - - Gathered adoption signals as a dedicated prompt section rather than modifying the existing objective format, keeping the diff minimal and focused on prompt/logic changes
  - - Used `gh api` for star/fork counts since the evidence exporter may not have run yet; this gives the groomer fresh data each run
  - - Assessed README structure locally (quickstart, demo, badge, goal) rather than requiring external tools, keeping the groomer self-contained
  - - Kept the existing balance rules in the prompt (1 in 5 must target adoption) and added concrete data so the LLM can generate specific, actionable adoption issues


### 2026-04-04 — [task-20260404-130317-add-github-stars-and-fork-count-as-tracked-objecti] (#124 kai-linux/agent-os)
Added GitHub stars and fork count as tracked objective metrics by creating `objectives/agent-os.yaml` with five weighted metrics (github_stars at 29%, github_forks at 14%, plus existing operational metrics), a lightweight `bin/export_github_evidence.sh` that fetches current counts via `gh api` and writes YAML evidence files plus a JSONL history log, and a fix to `_allowed_research_file` to support tilde-expanded evidence paths. The existing objective system automatically integrates these metrics into production feedback, outcome attribution snapshots, and the planner prompt.

**Files:** `- objectives/agent-os.yaml`, `- bin/export_github_evidence.sh`, `- orchestrator/strategic_planner.py`

**Decisions:**
  - - Used the existing objective file format so metrics auto-integrate into production feedback, outcome attribution, and planner prompts without additional code
  - - Weighted github_stars at 29% (matching task_success_rate) and github_forks at 14% to reflect stars as the primary adoption proxy with forks as secondary
  - - Evidence exporter writes both point-in-time YAML snapshots (for the objectives system) and a JSONL history log (for trend analysis)
  - - Fixed _allowed_research_file to handle tilde-expanded paths rather than changing evidence paths to be relative, since the evidence directory is intentionally outside the repo


### 2026-04-04 — [task-20260404-130220-create-a-compelling-visual-demo-showing-agentos-sh] (#123 kai-linux/agent-os)
Created an animated SVG terminal demo (docs/demo.svg) showing a real AgentOS task execution end-to-end — issue #115 "Cluster CI failures by error signature" flowing through dispatch → agent execution → PR #122 → CI pass → merge → closure. The animation uses SMIL which renders natively on GitHub. Embedded it prominently in README.md above the fold with links to the real issue and PR.

**Files:** `- docs/demo.svg`, `- README.md`

**Decisions:**
  - - Chose animated SVG with SMIL over GIF/video because it renders natively in GitHub READMEs without external hosting, is resolution-independent, and has negligible file size
  - - Used real task data (issue #115, PR #122, actual file names and test counts) rather than synthetic examples for credibility
  - - Placed demo between tagline and Goal section for maximum above-the-fold visibility
  - - Used Tokyo Night color scheme for terminal aesthetic that matches developer expectations
  - - Kept animation duration to ~17 seconds for quick comprehension on first view


### 2026-04-04 — [task-20260404-130122-condense-readme-into-a-scannable-1-page-pitch-with] (#125 kai-linux/agent-os)
Condensed the 710-line README into a 144-line scannable 1-page pitch with star badge, social proof callout, philosophy section, recursive self-improvement story, quick start, and capability ladder. All detailed documentation (architecture, execution, configuration/objectives, roadmap) was preserved by moving it to docs/.

**Files:** `- README.md`, `- docs/architecture.md`, `- docs/execution.md`, `- docs/configuration.md`, `- docs/roadmap.md`

**Decisions:**
  - - Split detailed docs into 4 files by topic (architecture, execution, configuration, roadmap) rather than one monolithic docs file
  - - Kept the philosophy section and recursive self-improvement story as dedicated sections in the pitch rather than burying them
  - - Used a blockquote for the social proof callout to make it visually distinct
  - - Preserved the ASCII loop diagram in a slightly condensed form since it's the key visual


### 2026-04-01 — [task-20260401-120519-cluster-ci-failures-by-error-signature-to-deduplic] (#115 kai-linux/agent-os)
Added conservative CI failure signature extraction and used it to cluster duplicate ready debug issues behind one primary task while keeping dependent issues for audit, then auto-resolved those dependents when the parent fix completed.

**Files:** `- orchestrator/ci_failure_signatures.py`, `- orchestrator/github_dispatcher.py`, `- orchestrator/github_sync.py`, `- tests/test_github_dispatcher.py`, `- tests/test_github_sync.py`, `- .agent_result.md`

**Decisions:**
  - - Kept signature matching conservative by requiring at least two anchors from error type, code location, and stack frame before clustering.
  - - Reused existing dependency handling by attaching duplicate issues to a primary via `Depends on #N` plus an explicit duplicate-parent marker instead of adding a new queue state.


### 2026-04-01 — [task-20260401-120417-implement-automatic-escalation-for-over-retried-bl] (#113 kai-linux/agent-os)
Added dispatcher-side automatic escalation for repeatedly blocked unowned tasks, including deduped GitHub comments, Telegram retry/close/skip decision cards, and structured escalation context built from task lineage and attempt logs.

**Files:** `- orchestrator/github_dispatcher.py`, `- orchestrator/queue.py`, `- tests/test_github_dispatcher.py`, `- tests/test_queue.py`, `- .agent_result.md`

**Decisions:**
  - - Kept the new escalation in the dispatcher so blocked tasks can be surfaced for human review without changing queue routing or mailbox task state.
  - - Reused the existing Telegram action store and dispatcher retry-decision note flow instead of introducing a second human-decision persistence path.


### 2026-04-01 — [task-20260401-120316-add-agent-health-checks-to-task-dispatch-routing] (#112 kai-linux/agent-os)
Added a cached 24-hour agent health gate to dispatch routing so agents at or below an 80% recent success rate are skipped during fallback evaluation, and dispatch now blocks with an explicit human-review escalation when no healthy candidate remains.

**Files:** `- orchestrator/agent_scorer.py`, `- orchestrator/queue.py`, `- orchestrator/github_dispatcher.py`, `- tests/test_queue.py`, `- tests/test_github_dispatcher.py`, `- .agent_result.md`

**Decisions:**
  - - Reused `runtime/metrics/agent_stats.jsonl` and the existing scorer parsing logic instead of adding a new health datastore.
  - - Treated agents with no recent 24-hour metrics as eligible so the new gate removes degraded agents without breaking routing for agents that lack fresh history.


### 2026-04-01 — [task-20260401-120115-fix-deepseek-auth-failures-in-agent-os] (#91 kai-linux/agent-os)
DeepSeek was being treated as dispatchable whenever an OpenRouter config directory existed, even if `secrets.json` was missing a usable `openRouterApiKey`. The queue now preflights that credential before dispatch, skips DeepSeek when auth is unavailable, and falls through to the next configured fallback agent instead of burning an execution attempt on a predictable authentication failure.

**Files:** `- .agent_result.md`, `- README.md`, `- orchestrator/queue.py`, `- tests/test_queue.py`

**Decisions:**
  - - Reused the existing `agent_available()` gate so the queue skips DeepSeek before execution rather than adding duplicate auth handling inside the runner and queue.
  - - Scoped credential validation to the known failing OpenRouter path and preserved the existing DeepSeek provider fallback behavior for NanoGPT and Chutes.


### 2026-03-31 — [task-20260331-113316-follow-up-partial-debug-for-task-20260331-112615-f] (#106 kai-linux/agent-os)
Validated that the existing follow-up remediation flow on this branch preserves the prior failing CI job name in GitHub debugging follow-up context and that queue-side CI verification still requires that preserved job name for rerun validation.

**Files:** `- .agent_result.md`

**Decisions:**
  - - Kept the diff limited to the required result artifact because the relevant code fix was already present on the assigned branch.
  - - Used focused regression tests instead of re-running full CI reproduction or the full test suite, since prior work had already covered those broader checks.


### 2026-03-31 — [task-20260331-105520-escalate-blocked-tasks-with-no-assigned-agent-with] (#57 kai-linux/agent-os)
Added a dispatcher-cycle safeguard that marks blocked mailbox tasks with `agent=none` on first sight and escalates them on the next cycle with a structured escalation note carrying the task id and blocker context.

**Files:** `- orchestrator/github_dispatcher.py`, `- tests/test_github_dispatcher.py`, `- .agent_result.md`

**Decisions:**
  - - Kept the change in the dispatcher cycle instead of the queue so existing blocked mailbox tasks can be recovered without widening worker behavior
  - - Reused the existing `escalated/` mailbox state and escalation-note pattern rather than introducing a new persistence path


### 2026-03-31 — [task-20260331-105617-integrate-production-feedback-metrics-into-task-sc] (#95 kai-linux/agent-os)
Integrated fresh PRODUCTION_FEEDBACK.md signals into strategic planner backlog ranking and post-plan prioritization so recent failures, blocker patterns, and recovery signals can reorder candidates, raise priority, and be cited directly in plan rationale.

**Files:** `- orchestrator/strategic_planner.py`, `- tests/test_strategic_planner.py`, `- .agent_result.md`

**Decisions:**
  - - Reused the existing PRODUCTION_FEEDBACK.md artifact format and parsed only its stable headings/bullets instead of redesigning the artifact
  - - Kept the integration bounded to lightweight keyword scoring and one-step priority promotion so production evidence influences planning without replacing the planner model


### 2026-03-31 — [task-20260331-105417-prevent-invalid-agent-assignments-in-task-dispatch] (#94 kai-linux/agent-os)
Dispatcher-side agent validation now rejects invalid agent preferences and blocks issues when no configured agent is currently available, preventing mailbox tasks from being created with impossible assignments like `agent=none`.

**Files:** `- orchestrator/github_dispatcher.py`, `- tests/test_github_dispatcher.py`, `- .agent_result.md`

**Decisions:**
  - - Reused the queue's existing agent availability checks via a lazy wrapper instead of introducing a second availability source.
  - - Kept the failure handling in the dispatcher's existing skip/block path so unavailable-agent tasks become explicit blocked issues rather than silent drops or inbox artifacts.


### 2026-03-31 — [task-20260331-105322-add-a-goal-section-to-readme-md] (#48 kai-linux/agent-os)
Added a concise Goal section near the top of README.md so planners and workers can prioritize work against the core product objective without changing the rest of the document structure.

**Files:** `- README.md`, `- .agent_result.md`

**Decisions:**
  - - Placed the Goal section directly after the opening product description so it is visible before implementation details
  - - Reused the repository's existing strategic language around credibility, trusted adoption, reliability, and evidence-driven improvement


### 2026-03-20 — [task-20260320-161412-backfill-the-current-sprint-production-feedback-ar] (#86 kai-linux/agent-os)
deepseek failed before producing a valid result file. Runner exited with code 1 while executing `/home/kai/agent-os/bin/agent_runner.sh deepseek /srv/worktrees/agent-os/task-20260320-161412-backfill-the-current-sprint-production-feedback-ar /home/kai/agent-os/runtime/tmp/task-20260320-161412-backfill-the-current-sprint-production-feedback-ar.txt`. Classified as: authentication failure. Orchestrator rescued and pushed the worktree changes.

**Files:** `- Unknown / inspect worktree`

**Decisions:**
  - - Treat runner failure as model-level failure and continue fallback chain if possible.
  - - Queue performed git rescue after the agent left valid changes behind.


### 2026-03-20 — [task-20260320-161311-record-explicit-unblock-decision-for-blocked-escal] (#79 kai-linux/agent-os)
deepseek failed before producing a valid result file. Runner exited with code 1 while executing `/home/kai/agent-os/bin/agent_runner.sh deepseek /srv/worktrees/agent-os/task-20260320-161311-record-explicit-unblock-decision-for-blocked-escal /home/kai/agent-os/runtime/tmp/task-20260320-161311-record-explicit-unblock-decision-for-blocked-escal.txt`. Classified as: authentication failure. Orchestrator rescued and pushed the worktree changes.

**Files:** `- Unknown / inspect worktree`

**Decisions:**
  - - Treat runner failure as model-level failure and continue fallback chain if possible.
  - - Queue performed git rescue after the agent left valid changes behind.


### 2026-03-20 — [task-20260320-161211-backfill-first-planning-research-artifact-for-curr] (#77 kai-linux/agent-os)
deepseek failed before producing a valid result file. Runner exited with code 1 while executing `/home/kai/agent-os/bin/agent_runner.sh deepseek /srv/worktrees/agent-os/task-20260320-161211-backfill-first-planning-research-artifact-for-curr /home/kai/agent-os/runtime/tmp/task-20260320-161211-backfill-first-planning-research-artifact-for-curr.txt`. Classified as: authentication failure. Orchestrator rescued and pushed the worktree changes.

**Files:** `- Unknown / inspect worktree`

**Decisions:**
  - - Treat runner failure as model-level failure and continue fallback chain if possible.
  - - Queue performed git rescue after the agent left valid changes behind.


### 2026-03-20 — [task-20260320-161116-make-agent-scorer-drive-closed-loop-remediation] (#47 kai-linux/agent-os)
Updated the degradation scorer into a bounded remediation-finding generator that classifies likely causes from existing metrics, scopes findings to the affected repo, and feeds the log analyzer concrete next steps for safer, more actionable self-improvement issues.

**Files:** `- orchestrator/agent_scorer.py`, `- orchestrator/log_analyzer.py`, `- tests/test_agent_scorer.py`, `- tests/test_log_analyzer.py`

**Decisions:**
  - - Reused existing `blocker_code`, `github_repo`, and success-rate metrics instead of adding new telemetry for degradation-cause classification.
  - - Kept the remediation loop bounded by emitting at most one cause-specific finding per degraded agent/repo combination and relying on existing log-analyzer dedupe before issue creation.


### 2026-03-20 — [task-20260320-161013-gate-ci-debug-task-closure-on-a-verified-green-rer] (#85 kai-linux/agent-os)
Added a queue-side PR CI remediation completion gate so debugging tasks only stay complete when GitHub records a post-attempt workflow rerun and the previously failing job is green.

**Files:** `- orchestrator/queue.py`, `- orchestrator/github_dispatcher.py`, `- tests/test_queue.py`, `- .agent_result.md`

**Decisions:**
  - - Kept the enforcement in `orchestrator/queue.py` so task closure is blocked before the queue moves a remediation task to `DONE`.
  - - Reused GitHub Actions run/job metadata instead of adding new persistence or PR-monitor state to verify rerun timing and job outcomes.


### 2026-03-20 — [task-20260320-134213-auto-rerun-blocked-tasks-after-prompt-inspection-c] (#78 kai-linux/agent-os)
Added a bounded queue-side recovery path that requeues a previously blocked task once after a linked prompt-inspection task completes successfully, and records recovery linkage on both the blocked attempt and the rerun task.

**Files:** `- orchestrator/queue.py`, `- tests/test_queue.py`, `- .agent_result.md`

**Decisions:**
  - - Kept the recovery logic inside the queue so it can requeue mailbox tasks without adding a separate dispatcher flow
  - - Limited automatic recovery to prompt-related blocked attempts by gating on `invalid_result_contract` and one-time requeue markers


### 2026-03-20 — [task-20260320-134110-auto-generate-production-feedback-md-each-sprint-c] (#76 kai-linux/agent-os)
Strategic planning now auto-generates `PRODUCTION_FEEDBACK.md` from repo-local runtime feedback data each sprint cycle, including explicit no-signal summaries when no substrate data exists.

**Files:** `- orchestrator/strategic_planner.py`, `- tests/test_strategic_planner.py`, `- .agent_result.md`

**Decisions:**
  - - Auto-enabled substrate-backed production feedback only when no explicit production-feedback config exists, while still respecting explicit `enabled: false`
  - - Reused existing runtime metrics and outcome attribution logs instead of adding a new storage path or job


### 2026-03-20 — [task-20260320-134012-collapse-self-improvement-generators-behind-one-ev] (#63 kai-linux/agent-os)
Collapsed overlapping self-improvement issue generation behind `log_analyzer.py` by turning `agent_scorer.py` into a structured finding emitter, removing queue-side remediation issue creation, and adding bounded evidence/reasoning to synthesized remediation issues.

**Files:** `- orchestrator/agent_scorer.py`, `- orchestrator/log_analyzer.py`, `- orchestrator/queue.py`, `- tests/test_log_analyzer.py`, `- tests/test_queue.py`, `- README.md`, `- bin/run_agent_scorer.sh`, `- bin/run_log_analyzer.sh`

**Decisions:**
  - - Reused existing metrics and queue artifacts, with one persisted scorer findings artifact, instead of adding new telemetry sources
  - - Kept all remediation issue creation inside `log_analyzer.py` so duplicate suppression and audit formatting happen in one place


### 2026-03-20 — [task-20260320-133911-preflight-git-push-readiness-before-dispatching-pu] (#50 kai-linux/agent-os)
Added a pre-dispatch push-readiness check for publish-requiring issues so the dispatcher blocks those tasks with a dedicated machine-readable `push_not_ready` classification when the runtime or repo is not push-capable.

**Files:** `- orchestrator/github_dispatcher.py`, `- tests/test_github_dispatcher.py`, `- .agent_result.md`

**Decisions:**
  - - Reused the existing dispatcher skip comment and blocked-label path instead of introducing a new persistence mechanism.
  - - Kept push readiness bounded to minimum local prerequisites: push enabled, git available, local repo present, git metadata writable, and `origin` configured.


### 2026-03-20 — [task-20260320-133809-add-ci-artifact-capture-for-failing-pr-jobs] (#45 kai-linux/agent-os)
Updated the existing CI workflow so pull request job failures upload a temporary artifact bundle containing dependency-install, lint, and pytest logs plus a pytest JUnit report when available.

**Files:** `- .github/workflows/ci.yml`, `- .agent_result.md`

**Decisions:**
  - - Kept the diff limited to the existing CI workflow file used by pull requests instead of adding a new workflow or helper script
  - - Captured logs directly in each failure-prone step so the uploaded artifact identifies the failing command without requiring a rerun


### 2026-03-20 — [task-20260320-121210-auto-generate-planning-research-md-each-sprint-cyc] (#56 kai-linux/agent-os)
deepseek failed before producing a valid result file. Runner exited with code 1 while executing `/home/kai/agent-os/bin/agent_runner.sh deepseek /srv/worktrees/agent-os/task-20260320-121210-auto-generate-planning-research-md-each-sprint-cyc /home/kai/agent-os/runtime/tmp/task-20260320-121210-auto-generate-planning-research-md-each-sprint-cyc.txt`. Classified as: authentication failure. Orchestrator rescued and pushed the worktree changes.

**Files:** `- Unknown / inspect worktree`

**Decisions:**
  - - Treat runner failure as model-level failure and continue fallback chain if possible.
  - - Queue performed git rescue after the agent left valid changes behind.


### 2026-03-20 — [task-20260320-121113-replace-static-fallback-chains-with-adaptive-agent] (#65 kai-linux/agent-os)
codex failed before producing a valid result file. Runner exited with code 1 while executing `/home/kai/agent-os/bin/agent_runner.sh codex /srv/worktrees/agent-os/task-20260320-121113-replace-static-fallback-chains-with-adaptive-agent /home/kai/agent-os/runtime/tmp/task-20260320-121113-replace-static-fallback-chains-with-adaptive-agent.txt`. Classified as: usage limit / rate limit. Orchestrator rescued and pushed the worktree changes.

**Files:** `- Unknown / inspect worktree`

**Decisions:**
  - - Treat runner failure as model-level failure and continue fallback chain if possible.
  - - Queue performed git rescue after the agent left valid changes behind.


### 2026-03-20 — [task-20260320-121011-quarantine-tasks-that-block-repeatedly-within-one-] (#59 kai-linux/agent-os)
deepseek failed before producing a valid result file. Runner exited with code 1 while executing `/home/kai/agent-os/bin/agent_runner.sh deepseek /srv/worktrees/agent-os/task-20260320-121011-quarantine-tasks-that-block-repeatedly-within-one- /home/kai/agent-os/runtime/tmp/task-20260320-121011-quarantine-tasks-that-block-repeatedly-within-one-.txt`. Classified as: authentication failure. Orchestrator rescued and pushed the worktree changes.

**Files:** `- Unknown / inspect worktree`

**Decisions:**
  - - Treat runner failure as model-level failure and continue fallback chain if possible.
  - - Queue performed git rescue after the agent left valid changes behind.


### 2026-03-20 — [task-20260320-120911-add-regression-test-for-pr-ci-failure-recovery-flo] (#55 kai-linux/agent-os)
Added a focused regression test that simulates a partial outcome from a PR CI remediation task and verifies the recovery handoff creates a ready follow-up issue on the same PR branch while the active remediation issue moves to blocked.

**Files:** `- tests/test_github_sync.py`, `- .agent_result.md`

**Decisions:**
  - - Kept the diff limited to test coverage because the existing implementation already satisfies the intended PR CI recovery flow
  - - Targeted `github_sync.sync_result()` as the recovery handoff seam where a partial remediation outcome must create the correct follow-up state


### 2026-03-20 — [task-20260320-120812-consume-escalation-note-retry-decisions-in-task-di] (#53 kai-linux/agent-os)
Implemented structured escalation retry-decision parsing in the dispatcher and wired retry, reroute, and stop actions to the originating blocked task record with traceability fields and bounded GitHub status updates.

**Files:** `- orchestrator/github_dispatcher.py`, `- tests/test_github_dispatcher.py`, `- .agent_result.md`

**Decisions:**
  - - Kept the change bounded to dispatcher-side parsing and task/action wiring instead of redesigning the escalation note producer
  - - Stored action and reason directly on the originating task frontmatter and marked notes as applied to preserve traceability and avoid repeated execution


### 2026-03-20 — [task-20260320-113613-fix-ci-failure-on-pr-71] (#73 kai-linux/agent-os)
Repaired the PR #71 CI failure in the worktree by resolving committed merge-conflict markers in `orchestrator/github_dispatcher.py` and `tests/test_github_dispatcher.py`, preserving both intended behaviors: fallback branch-field retention and outcome-check propagation; local verification is green, but commit/push is blocked by sandbox denial on the worktree Git admin directory. Orchestrator rescued and pushed the worktree changes.

**Files:** `- .agent_result.md`, `- orchestrator/github_dispatcher.py`, `- tests/test_github_dispatcher.py`

**Decisions:**
  - - Kept the smallest viable diff by resolving the accidental merge conflicts instead of refactoring dispatcher parsing
  - - Combined both sides of the conflicted changes so the dispatcher still backfills missing raw issue fields and still emits outcome check IDs
  - - Queue performed git rescue after the agent left valid changes behind.


### 2026-03-20 — [task-20260320-112312-fix-ci-failure-on-pr-71] (#73 kai-linux/agent-os)
CI was failing because the PR branch still contained unresolved merge-conflict markers in `orchestrator/strategic_planner.py`, `tests/test_strategic_planner.py`, and `README.md`, which broke pytest collection. I resolved the conflicted sections by keeping both the production-feedback and outcome-attribution behavior, aligned the planner prompt/tests to the merged signature, and verified the full test suite passes locally, but I could not commit or push because this environment is denied access to the worktree git lockfile path. Orchestrator rescued and pushed the worktree changes.

**Files:** `- .agent_result.md`, `- README.md`, `- orchestrator/strategic_planner.py`, `- tests/test_strategic_planner.py`

**Decisions:**
  - - Resolved the conflict by preserving both features instead of reverting either side, because the branch was meant to include production feedback and post-merge outcome attribution together.
  - - Kept the diff minimal and limited to the conflicted files plus the required task result artifact.
  - - Queue performed git rescue after the agent left valid changes behind.


### 2026-03-20 — [task-20260320-105114-fix-ci-failure-on-pr-71] (#73 kai-linux/agent-os)
deepseek failed before producing a valid result file. Runner exited with code 1 while executing `/home/kai/agent-os/bin/agent_runner.sh deepseek /srv/worktrees/agent-os/task-20260320-105114-fix-ci-failure-on-pr-71 /home/kai/agent-os/runtime/tmp/task-20260320-105114-fix-ci-failure-on-pr-71.txt`. Classified as: authentication failure. Orchestrator rescued and pushed the worktree changes.

**Files:** `- Unknown / inspect worktree`

**Decisions:**
  - - Treat runner failure as model-level failure and continue fallback chain if possible.
  - - Queue performed git rescue after the agent left valid changes behind.


### 2026-03-20 — [task-20260320-101212-auto-file-bounded-follow-ups-for-partial-debug-out] (#60 kai-linux/agent-os)
Implemented deduped GitHub follow-up creation for partial debugging outcomes and suppressed the local mailbox stub when that GitHub follow-up exists, with a focused regression test covering create-once behavior.

**Files:** `- orchestrator/github_sync.py`, `- orchestrator/queue.py`, `- tests/test_github_sync.py`

**Decisions:**
  - - Reused the existing GitHub sync path for issue-backed tasks so partial debugging follow-ups become real GitHub issues without introducing a second orchestration flow
  - - Kept mailbox follow-up creation unchanged for non-debugging or non-GitHub-backed tasks to minimize diff and behavioral risk


### 2026-03-20 — [task-20260320-101116-add-post-merge-outcome-attribution-for-issue-pr-an] (#64 kai-linux/agent-os)
Added bounded post-merge outcome attribution by carrying issue-defined outcome check IDs through dispatch, recording task/issue/PR attribution events and timestamped outcome snapshots in a durable JSONL log, and surfacing that evidence in planner retrospectives and sprint-planning prompts with explicit inconclusive handling when no measurable external metric exists.

**Files:** `- README.md`, `- example.config.yaml`, `- orchestrator/github_dispatcher.py`, `- orchestrator/github_sync.py`, `- orchestrator/outcome_attribution.py`, `- orchestrator/pr_monitor.py`, `- orchestrator/strategic_planner.py`, `- tests/test_github_dispatcher.py`

**Decisions:**
  - - Reused existing task, issue, branch, and PR identifiers and logged attribution alongside the existing runtime metrics directory instead of creating a separate identity or storage system.
  - - Kept the first version bounded to configured file/web outcome sources and one delayed snapshot per merged check, with `inconclusive` as the explicit fallback for missing, unreadable, or non-measurable outcomes.


### 2026-03-20 — [task-20260320-101013-build-a-normalized-production-feedback-substrate-f] (#62 kai-linux/agent-os)
Added a first-class file-based production feedback substrate that refreshes bounded repo-local evidence into `PRODUCTION_FEEDBACK.md`, guards stale/low-trust/privacy-sensitive inputs, and injects the resulting artifact into planning, backlog grooming, and evidence-heavy execution context while keeping legacy `planning_signals` config working.

**Files:** `- .gitignore`, `- .agent_result.md`, `- CODEBASE.md`, `- README.md`, `- example.config.yaml`, `- orchestrator/backlog_groomer.py`, `- orchestrator/repo_context.py`, `- orchestrator/strategic_planner.py`

**Decisions:**
  - - Reused the existing bounded planning-signals refresh path and layered repo-context pattern instead of creating a new memory subsystem
  - - Kept legacy `planning_signals` config support so existing repos can migrate incrementally
  - - Made stale, low-trust, and privacy-sensitive inputs inspectable but guarded inside the artifact instead of silently dropping them
  - - Kept the first version file-based and repo-opt-in through `production_feedback` config and `PRODUCTION_FEEDBACK.md`

Added a first-class, file-based production feedback substrate that refreshes bounded repo-local evidence into `PRODUCTION_FEEDBACK.md`, applies freshness/trust/privacy guardrails, and injects the artifact into strategic planning, backlog grooming, and evidence-heavy execution prompts while preserving legacy `planning_signals` compatibility.

**Files:** `- orchestrator/strategic_planner.py`, `- orchestrator/repo_context.py`, `- orchestrator/backlog_groomer.py`, `- README.md`, `- example.config.yaml`, `- .gitignore`, `- tests/test_strategic_planner.py`, `- tests/test_backlog_groomer.py`, `- tests/test_queue.py`

**Decisions:**
  - - Reused the existing planning-signals refresh path and layered repo-context model instead of introducing a separate memory or ingestion subsystem
  - - Guarded stale, low-trust, and privacy-sensitive inputs in the artifact itself so evidence stays inspectable without silently driving planning

### 2026-03-20 — [task-20260320-100911-require-structured-blocker-codes-on-blocked-task-o] (#54 kai-linux/agent-os)
Added bounded `BLOCKER_CODE` validation and persistence for blocked and partial task outcomes, updated queue-generated fallback outcomes to emit valid codes, and documented the contract without breaking existing readers that ignore unknown fields.

**Files:** `- orchestrator/queue.py`, `- orchestrator/github_sync.py`, `- tests/test_queue.py`, `- README.md`

**Decisions:**
  - - Kept the change bounded to the queue contract and downstream persistence points instead of introducing a separate schema module
  - - Used `invalid_result_contract` as the enforcement path when a blocked or partial outcome omits or misstates `BLOCKER_CODE`


### 2026-03-20 — [task-20260320-100812-persist-worker-prompt-snapshots-for-each-dispatche] (#51 kai-linux/agent-os)
Persisted each task run's exact final worker prompt to a stable `runtime/prompts/<task_id>.txt` artifact and linked that artifact from task frontmatter so blocked and partial runs keep a reproducible prompt snapshot without agent-specific handling.

**Files:** `- orchestrator/paths.py`, `- orchestrator/github_dispatcher.py`, `- orchestrator/queue.py`, `- tests/test_github_dispatcher.py`, `- tests/test_queue.py`, `- .agent_result.md`

**Decisions:**
  - - Reused existing task metadata and runtime directories instead of introducing a new prompt-tracking subsystem.
  - - Stored the final prompt snapshot by task id and rewrote it on each prompt regeneration so the durable artifact always reflects the exact last prompt actually used.


### 2026-03-20 — [task-20260320-081208-gate-git-publish-tasks-on-writable-remote-capabili] (#58 kai-linux/agent-os)
Added a dispatch-time publish capability gate so tasks that explicitly require commit/push or PR publication are blocked before entering the worker inbox when `default_allow_push` is disabled, with a machine-readable skip reason recorded on the issue.

**Files:** `- orchestrator/github_dispatcher.py`, `- tests/test_github_dispatcher.py`, `- .agent_result.md`

**Decisions:**
  - - Recorded the skip reason both as a dedicated issue label (`dispatch:missing-publish-capability`) and as structured JSON in an issue comment so downstream automation can route on either signal.
  - - Kept detection conservative and text-based in the dispatcher instead of adding a broader capability framework, which preserves the minimal diff requested.


### 2026-03-19 — [task-20260319-215808-integrate-analytics-and-user-signal-inputs-into-pl] (#41 kai-linux/agent-os)
Added a bounded planning-signals path to the strategic planner so repos can opt into analytics, user-feedback, and market inputs, normalize them into `PLANNING_SIGNALS.md` with freshness/provenance/trust/privacy metadata, and use that evidence during sprint selection and evidence-heavy execution work.

**Files:** `- .agent_result.md`, `- .gitignore`, `- CODEBASE.md`, `- README.md`, `- example.config.yaml`, `- orchestrator/repo_context.py`, `- orchestrator/strategic_planner.py`, `- tests/test_queue.py`

**Decisions:**
  - - Kept the first version bounded to one opt-in `planning_signals` artifact and three explicit input types instead of adding a general ingestion framework
  - - Reused the existing safe web/file source model and required trust/privacy notes in the normalized artifact so public repos can use external evidence without assuming private raw analytics access

Added bounded planning signals to the strategic planner. Repos can now opt into explicit analytics, user feedback, and market inputs that normalize into a local `PLANNING_SIGNALS.md` artifact with timestamps, provenance, freshness, trust/privacy notes, extracted metrics, and planning implications; that artifact is injected into strategic planning and evidence-heavy worker prompts.

**Files:** `- .gitignore`, `- README.md`, `- example.config.yaml`, `- orchestrator/strategic_planner.py`, `- orchestrator/repo_context.py`, `- tests/test_strategic_planner.py`, `- tests/test_queue.py`

**Decisions:**
  - - Kept the first version bounded to three explicit input types (`analytics`, `user_feedback`, `market_signal`) instead of introducing a generic ingestion layer
  - - Reused the existing safe web/file source model and required trust/privacy metadata in the normalized artifact so public repos can consume external signals without assuming private raw analytics access

### 2026-03-19 — [task-20260319-205609-add-pre-planning-research-inputs-to-strategic-plan] (#39 kai-linux/agent-os)
Added an opt-in pre-planning research phase to the strategic planner. It now refreshes a bounded `PLANNING_RESEARCH.md` artifact from explicitly configured trusted web and local sources before sprint selection, then injects that structured research context into the planning prompt.

**Files:** `- .gitignore`, `- README.md`, `- example.config.yaml`, `- orchestrator/strategic_planner.py`, `- tests/test_strategic_planner.py`

**Decisions:**
  - - Kept research opt-in and tightly bounded to configured `https` URLs plus relative repo or repo-adjacent files; no search or uncontrolled browsing path was added
  - - Wrote research into a local `PLANNING_RESEARCH.md` artifact and ignored it in git so planning can reuse fresh evidence without adding commit churn


### 2026-03-19 — [task-20260319-155323-multi-repo-strategic-planning-cross-repo-dependenc] (#24 kai-linux/agent-os)
Implemented multi-repo strategy preloading, conservative cross-repo dependency inference, dependency-aware planning order, and prompt context injection so sprint planning can sequence prerequisite repository work before dependent repository work.

**Files:** `- orchestrator/strategic_planner.py`, `- tests/test_strategic_planner.py`, `- .agent_result.md`

**Decisions:**
  - - Used conservative dependency inference from explicit dependency headings and phrases instead of broad repo-name matching to avoid false positives
  - - Reordered repo planning with a topological pass so prerequisite repos are planned first while preserving a deterministic fallback order for cycles or missing links


### 2026-03-19 — [task-20260319-133023-strategic-planner-configurable-plan-size-and-sprin] (#25 kai-linux/agent-os)
Made plan size and sprint cadence configurable per-repository. Added `plan_size` and `sprint_cadence_days` as top-level config fields with per-repo overrides via `github_projects` repos entries. The retrospective window automatically adjusts to match the configured sprint cadence. Defaults to current behavior (5 tasks, 7 days) when not configured.

**Files:** `- orchestrator/strategic_planner.py`, `- tests/test_strategic_planner.py`, `- example.config.yaml`

**Decisions:**
  - - Used top-level `plan_size` and `sprint_cadence_days` as global defaults, with per-repo overrides in `github_projects.*.repos[].plan_size` and `github_projects.*.repos[].sprint_cadence_days` — matches existing per-repo config patterns
  - - Renamed `PLAN_SIZE` to `DEFAULT_PLAN_SIZE` to clarify it's a fallback, not a fixed value
  - - Made `_build_retrospective()` accept a `days` parameter rather than reading config itself — keeps the function pure and testable


### 2026-03-19 — [task-20260319-103913-strategy-md-auto-update-focus-areas-from-sprint-pa] (#23 kai-linux/agent-os)
Added automatic focus area extraction to the strategic planner. When STRATEGY.md has 3+ sprint entries, a Haiku LLM call analyzes sprint history to identify 3-5 recurring work themes and updates the 'Current Focus Areas' section. User-edited content is preserved via an HTML comment marker (`<!-- auto-focus-areas -->`); if the marker is absent and the section contains non-placeholder content, the update is skipped.

**Files:** `- orchestrator/strategic_planner.py`, `- tests/test_strategic_planner.py`

**Decisions:**
  - - Used an HTML comment marker (`<!-- auto-focus-areas -->`) to distinguish auto-generated focus areas from manually edited ones — preserves user content without requiring a separate config flag
  - - Haiku model used for focus area analysis (cheap, fast) while planning stays on the existing model
  - - Capped sprint entries sent to Haiku at 10 most recent to keep prompts bounded
  - - Focus area analysis is non-blocking — failures are logged but don't prevent the strategy update


### 2026-03-19 — [task-20260319-103819-sprint-retrospective-quality-llm-generated-analysi] (#22 kai-linux/agent-os)
Implemented a task decomposer that analyzes incoming issues via Claude Haiku to determine if they are atomic tasks or epics. Atomic tasks pass through unchanged. Epics are automatically split into up to 5 ordered sub-issues with 'Part of #N' cross-references, the first sub-issue is dispatched immediately, and the rest are sent to Backlog. The decomposer is non-blocking — failures fall back to treating the issue as atomic.

**Files:** `- orchestrator/task_decomposer.py`, `- orchestrator/github_dispatcher.py`, `- tests/test_task_decomposer.py`

**Decisions:**
  - - Reused the existing structured-JSON Claude pattern from task_formatter.py so decomposition stays deterministic and cheap with Haiku
  - - Kept decomposition inside the dispatcher path (_dispatch_item) instead of adding another job or queue stage, which preserved the existing atomic-task flow and simplified fallback behavior
  - - Parent epic is closed after decomposition since work is tracked in sub-issues — avoids double-dispatch
  - - Used lazy imports for gh_project functions in task_decomposer.py to avoid circular imports


### 2026-03-19 — [task-20260319-101806-task-task-decomposer-agent] (#2 kai-linux/agent-os)
Implemented a task decomposer that analyzes incoming issues via Claude Haiku to determine if they are atomic tasks or epics. Atomic tasks pass through unchanged. Epics are automatically split into up to 5 ordered sub-issues with 'Part of #N' cross-references, the first sub-issue is dispatched immediately, and the rest are sent to Backlog. The decomposer is non-blocking — failures fall back to treating the issue as atomic.

**Files:** `- orchestrator/task_decomposer.py`, `- orchestrator/github_dispatcher.py`, `- tests/test_task_decomposer.py`

**Decisions:**
  - - Reused the existing structured-JSON Claude pattern from task_formatter.py so decomposition stays deterministic and cheap with Haiku
  - - Kept decomposition inside the dispatcher path (_dispatch_item) instead of adding another job or queue stage, which preserved the existing atomic-task flow and simplified fallback behavior
  - - Parent epic is closed after decomposition since work is tracked in sub-issues — avoids double-dispatch
  - - Used lazy imports for gh_project functions in task_decomposer.py to avoid circular imports


### 2026-03-19 — [task-20260319-073306-task-daily-digest-to-telegram] (#8 kai-linux/agent-os)
Implemented a daily digest job that reads the last 24 hours of mailbox outcomes and queue logs, computes per-agent success rates, counts agent PR creation and merges, and sends a compact Telegram summary with a no-activity fallback.

**Files:** `- orchestrator/daily_digest.py`, `- bin/run_daily_digest.sh`, `- tests/test_daily_digest.py`, `- CRON.md`, `- .agent_result.md`

**Decisions:**
  - - Used mailbox file timestamps to define the 24-hour window and `queue-summary.log` task segments to recover the final agent per task without mutating runtime state.
  - - Counted PR activity via read-only `gh pr list` queries across configured repos so created and merged counts remain accurate for the last 24 hours even though local logs do not carry reliable timestamps for both events.


### 2026-03-19 — [task-20260319-073206-task-structured-telegram-escalation-with-reply-but] (#7 kai-linux/agent-os)
Implemented Telegram escalation notifications with inline reply buttons, persisted 48-hour callback actions, and a long-poll callback handler that can re-queue escalated work or close the original issue as won't-fix from Telegram.

**Files:** `- orchestrator/queue.py`, `- orchestrator/supervisor.py`, `- orchestrator/paths.py`, `- tests/test_queue.py`

**Decisions:**
  - - Used Telegram long-polling inside the existing supervisor loop instead of adding a webhook service, which keeps the single-user bot setup minimal.
  - - Stored callback action state under `runtime/telegram_actions` with an update offset file so button presses stay durable across process restarts and expire after 48 hours.


### 2026-03-19 — [task-20260319-073106-task-task-dependency-resolver] (#6 kai-linux/agent-os)
Implemented dependency-aware dispatching in the GitHub dispatcher so issues declaring `Depends on #N` or `Blocked by #N` stay blocked until their dependency chain is resolved, then automatically return to `Ready` on later dispatcher runs.
### 2026-03-19 — [task-20260319-073106-task-task-dependency-resolver] (#6 kai-linux/agent-os)
Added dependency-aware dispatching to `orchestrator/github_dispatcher.py`. Issues can now declare `Depends on #N` or `Blocked by #N`; ready tasks are held in `Status=Blocked` with a `Waiting for #N` comment until dependencies close, blocked tasks are moved back to `Ready` on later dispatcher runs, and circular/deep dependency chains are skipped with warnings.

**Files:** `- orchestrator/github_dispatcher.py`, `- tests/test_github_dispatcher.py`, `- CODEBASE.md`, `- .agent_result.md`

**Decisions:**
  - - Reused project query results as the primary dependency cache to avoid unnecessary extra `gh` calls
  - - Capped dependency traversal at 3 levels and limited uncached remote lookups to one `gh issue view` per candidate

  - - Reused the existing project query payload as the primary dependency cache so most checks do not add extra `gh` calls
  - - Limited remote dependency lookups to one `gh issue view` per candidate and capped recursive dependency traversal at three levels

### 2026-03-18 — [task-20260318-224806-task-task-decomposer-agent] (#2 kai-linux/agent-os)
Added a fast Claude Haiku decomposition step to the dispatcher so atomic issues still dispatch unchanged while epic issues are split into ordered sub-issues, linked back to the parent, and the first child is dispatched immediately.

**Files:** `- orchestrator/task_decomposer.py`, `- orchestrator/github_dispatcher.py`, `- orchestrator/gh_project.py`, `- tests/test_task_decomposer.py`, `- example.config.yaml`, `- README.md`, `- CODEBASE.md`, `- .agent_result.md`

**Decisions:**
  - - Reused the existing structured-JSON Claude pattern from `task_formatter.py` so decomposition stays deterministic and cheap with Haiku
  - - Kept decomposition inside the dispatcher path instead of adding another job or queue stage, which preserved the existing atomic-task flow and simplified fallback behavior


### 2026-03-18 — [task-20260318-093604-task-auto-backlog-groomer] (#12 kai-linux/agent-os)
Implemented `orchestrator/backlog_groomer.py` and `bin/run_backlog_groomer.sh`. The groomer reads each repo's open issues (via `gh issue list`), last 30 days of `agent_stats.jsonl` completions, CODEBASE.md Known Issues section, and risk flags from `.agent_result.md` files in worktrees. It identifies stale issues (>30 days no activity), Known Issues without linked GitHub issues, and risk flags from recent completions. All data is sent to Claude Haiku with a deterministic prompt to generate 3-5 targeted improvement tasks. Semantic dedup via `difflib.SequenceMatcher` (0.75 threshold) prevents duplicate issues. A system cron fires every Saturday at 20:00.

**Files:** `- orchestrator/backlog_groomer.py`, `- bin/run_backlog_groomer.sh`

**Decisions:**
  - - Reused load_recent_metrics() from agent_scorer.py with 30-day window for completions
  - - Semantic dedup uses difflib.SequenceMatcher (0.75 threshold) rather than exact title match, preventing near-duplicate issues
  - - Deterministic Haiku prompt requests exact JSON schema with ## Goal / ## Success Criteria / ## Constraints body format for dispatcher compatibility
  - - Risk flags scanned from .agent_result.md files in worktrees directory (filtered by 30-day mtime)
  - - CODEBASE.md Known Issues parsed via regex between ## Known Issues heading and next ## heading


### 2026-03-18 — [task-20260318-082904-task-weekly-log-analyzer-auto-creates-improvement-] (#10 kai-linux/agent-os)
Implemented `orchestrator/log_analyzer.py` and `bin/run_log_analyzer.sh`. The analyzer reads the last 7 days of `runtime/metrics/agent_stats.jsonl` and `runtime/logs/queue-summary.log`, calls Claude Haiku with a deterministic structured prompt to identify the top 3 failure patterns/bottlenecks, deduplicates against existing open GitHub issues by exact title match, creates one issue per problem using the standard `## Goal / ## Success Criteria / ## Constraints` body format, and posts a summary to Telegram. A system cron fires every Monday at 07:00.

**Files:** `- orchestrator/log_analyzer.py`, `- bin/run_log_analyzer.sh`, `- CODEBASE.md`

**Decisions:**
  - - Reused load_recent_metrics() from agent_scorer.py to avoid duplicating JSONL parsing
  - - Prompt is fully deterministic (requests exact JSON schema, no open-ended fields) to avoid noisy/random issue titles across runs
  - - Deduplication uses gh issue list --search + exact title match to prevent duplicate improvement tasks
  - - Issue body uses standard ## Goal / ## Success Criteria / ## Constraints sections for dispatcher compatibility
  - - Added to system crontab (persistent) rather than session-only CronCreate
Added `orchestrator/log_analyzer.py` — a weekly analyzer that reads the last 7 days of `runtime/metrics/agent_stats.jsonl` and `runtime/logs/queue-summary.log`, sends both to Claude Haiku with a deterministic prompt to identify the top 3 failure patterns / bottlenecks, deduplicates against open GitHub issues, creates one issue per problem (body follows `## Goal / ## Success Criteria / ## Constraints` template), and posts a summary to Telegram. `bin/run_log_analyzer.sh` is the entry point; a system cron fires every Monday at 07:00.

**Files:** `- orchestrator/log_analyzer.py`, `- bin/run_log_analyzer.sh`

**Decisions:**
  - - Reuses `load_recent_metrics()` from `agent_scorer.py` to avoid duplicating the JSONL parsing logic
  - - Prompt is deterministic (asks for exact JSON schema, no open-ended options) to reduce noise in repeated runs
  - - Deduplication uses `gh issue list --search <title>` and exact-title match; avoids creating duplicate improvement tasks
  - - Issue body uses the standard `## Goal / ## Success Criteria / ## Constraints` sections so the dispatcher can route them normally
  - - Cron added to system crontab (`0 7 * * 1`) rather than relying on session-only CronCreate



### 2026-03-18 — [task-20260318-065704-task-agent-performance-scorer-and-metrics-log] (#9 kai-linux/agent-os)
Added structured metrics logging to queue.py and a new weekly agent performance scorer. After each task completes, `record_metrics()` atomically appends a JSONL record (timestamp, task_id, repo, agent, status, attempt_count, duration_seconds, task_type) to `runtime/metrics/agent_stats.jsonl`, with automatic rotation at 10 MB. A new `orchestrator/agent_scorer.py` module reads the last 7 days of metrics, computes per-agent success rates, and creates a GitHub issue with the title "Agent X degraded (Y% success rate)" for any agent below 60%. `bin/run_agent_scorer.sh` is the cron entry point.

**Files:** `- orchestrator/queue.py`, `- orchestrator/agent_scorer.py`, `- bin/run_agent_scorer.sh`

**Decisions:**
  - - Atomic append implemented as read-existing + write-temp + os.replace() per task spec ("write to temporary file then rename"); handles single-writer safety
  - - record_metrics() is wrapped in try/except in main() so metrics failure never crashes the queue
  - - Rotation renames to .jsonl.1 (overwriting previous rotation); simple single-rotation keeps implementation minimal
  - - agent_scorer.py uses `gh issue create` via subprocess (same pattern as rest of codebase) rather than a new HTTP client
  - - github_repo config key falls back to scanning github_projects entries for a "repo" field if top-level github_repo is not set


### 2026-03-18 — [task-20260318-064404-task-priority-aware-queue-dispatch] (#5 kai-linux/agent-os)
Implemented priority-aware queue dispatch by adding a `priority_score()` helper to `queue.py` that computes score = priority_weight + age_bonus (1 pt/hr), replacing the FIFO `pick_task` with a `max()` selector. The dispatcher writes `priority` into task frontmatter from issue labels (defaulting to `prio:normal`), and the queue logs the priority label, weight, and score when a task is picked up.

**Files:** `- orchestrator/queue.py`, `- orchestrator/github_dispatcher.py`, `- example.config.yaml`

**Decisions:**
  - - Used file mtime as task age proxy (set at dispatch time, stable across queue restarts)
  - - Added cfg=None fallback in pick_task so any call without cfg still works (backward compat)
  - - Priority logged at also_summary=True so it appears in queue summary log, not just per-task log
  - - Weights dict defaults hardcoded in priority_score itself to avoid KeyError if priority_weights missing from config


### 2026-03-17 — [task-20260317-221804-task-pr-auto-merge-on-green-ci] (#3 kai-linux/agent-os)
Implemented `orchestrator/pr_monitor.py` — a new module that lists open PRs with "Agent:" title prefix across all configured repos, checks CI status via `gh pr checks`, auto-merges on green using `gh pr merge --squash`, and posts a structured failure comment + adds "blocked" label + sets project Status=Blocked on CI failure. Merge attempts are tracked in a JSON state file (`runtime/logs/pr_monitor_state.json`); each PR is retried at most 3 times before being escalated. Also added `bin/run_pr_monitor.sh` as the entry-point for a cron job running every 5 minutes.

**Files:** `- orchestrator/pr_monitor.py`, `- bin/run_pr_monitor.sh`

**Decisions:**
  - - Used a JSON state file (not PR labels) to track merge attempts — avoids extra GitHub API calls and label clutter
  - - gh pr checks returns non-zero exit on failing checks but still outputs valid JSON; captured both outcomes via subprocess.run without check=True
  - - Pending checks (state: pending/queued/in_progress) cause the PR to be skipped until the next poll rather than counted as a failure
  - - CONFLICTING mergeable state skips without incrementing attempt counter (not a CI issue)
  - - Draft PRs are skipped silently without incrementing attempt counter


### 2026-03-17 — [task-20260317-214604-task-test-runner-agent-role] (#4 kai-linux/agent-os)
Implemented a test runner role by adding a `run_tests()` function to `orchestrator/queue.py` that executes a configured test command in the worktree after the agent finishes (just before `parse_agent_result` reads the result file). Test results are appended to `.agent_result.md`'s TESTS_RUN section; if tests fail and the agent reported `complete`, the status is overridden to `partial` and BLOCKERS are updated. Documented `test_command` and `test_timeout_minutes` in `example.config.yaml` with per-repo `repo_configs` support.

**Files:** `- orchestrator/queue.py`, `- example.config.yaml`

**Decisions:**
  - - Used `repo_configs` dict in config.yaml keyed by resolved repo path (falling back to repo.name) for per-repo test commands — minimal change, no new files
  - - Shell=True for test_command to support compound commands like `cd src && pytest`
  - - Test runner modifies `.agent_result.md` before `parse_agent_result()` reads it — queue routing sees the final overridden status directly, no routing logic changes needed
  - - Failures (subprocess errors, timeouts) caught and recorded as ERROR/TIMEOUT labels, never crash the queue


### 2026-03-17 — [task-20260317-213404-task-parallel-queue-workers] (#1 kai-linux/agent-os)
Implemented parallel queue worker execution by creating a thin `orchestrator/supervisor.py` that spawns up to `max_parallel_workers` independent queue.py processes. Per-repo file locking (`fcntl.flock`) was added to `queue.py` so that no two workers can access the same repository simultaneously. Workers are identified by a `QUEUE_WORKER_ID` environment variable that appears in logs. Single-worker mode (`max_parallel_workers=1`) is unchanged in behavior.

**Files:** `- example.config.yaml`, `- orchestrator/supervisor.py`, `- orchestrator/queue.py`, `- bin/run_queue.sh`

**Decisions:**
  - - Thin supervisor (new file) over refactoring queue.py to minimize diff and risk
  - - Per-repo lock uses /tmp lock files with fcntl (no new dependencies, same mechanism as global queue lock)
  - - Locked-repo workers return task to inbox and exit; supervisor respawns workers, so tasks eventually run when repo is free
  - - Global flock in run_queue.sh kept on supervisor to prevent duplicate supervisor instances
  - - Worker IDs (w0, w1, ...) are sequential integers from supervisor lifetime counter

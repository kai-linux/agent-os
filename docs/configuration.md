# Configuration

## Recursive Self-Improvement

This is the part that makes Agent OS different from a task runner.

Every Monday at 07:00, two things happen automatically:

1. **`agent_scorer.py`** computes execution degradation signals plus weighted business-outcome findings from external repo objectives and writes them to `runtime/analysis/agent_scorer_findings.json`. It does not open issues directly.

2. **`log_analyzer.py`** reads the last 7 days of execution metrics, the queue summary log, and the scorer findings artifact. It synthesizes exactly one remediation backlog from that evidence, deduplicates overlapping signals, and files GitHub issues with explicit `## Evidence` and `## Reasoning` sections.

Every Saturday at 20:00:

3. **`backlog_groomer.py`** scans every repo for stale issues (>30 days), Known Issues in `CODEBASE.md` that don't have linked tickets, and risk flags from recent agent results. It generates 3-5 new improvement tasks. Semantic deduplication (0.75 similarity threshold) prevents duplicate issues from piling up.

The planner and groomer are safe to invoke frequently from cron. Each repo has its own cadence in config (`sprint_cadence_days`, `groomer_cadence_days`), fractional days are supported, and `0` means dormant. The configured cadence is a minimum interval, not a scheduler by itself: the job only runs when cron invokes it, so cron must run at least as often as your shortest desired cadence.

Strategic planning also supports an explicit early-refresh policy. `planner_allow_early_refresh: true` lets a repo refresh its sprint before the next cadence tick when the current sprint has gone idle; `false` enforces strict cadence. The setting can be defined globally, per project, or per repo, with repo overrides winning.

Repos can also opt into a manual execution lane with `automation_mode: dispatcher_only`. In that mode, Agent OS still accepts human-created issues, dispatches work when you move an issue to `Ready`, and opens PRs on completion, but it skips the planner, backlog groomer, weekly analyzer/scorer issue generation, PR auto-merge/CI remediation, and queue-generated self-healing follow-up tasks for that repo.

The `bin/` entrypoints bootstrap common user-local CLI install paths themselves, so cron usually does not need per-provider `PATH` or `CLAUDE_BIN` overrides.

Configuration now defaults to the repo-local [config.yaml](/Users/kai/agent-os/config.yaml). Per-repo business objectives should live alongside it in an `objectives/` directory inside the repo, while raw analytics and other external evidence can still live in a separate external directory such as `~/.local/share/agent-os/evidence/`. This keeps the operational config obvious while still allowing external metric snapshots and private evidence stores.

Ownership is intentionally split:
- `agent_scorer.py` measures execution degradation plus business-outcome movement and emits structured findings only.
- `log_analyzer.py` owns evidence synthesis, prioritization, deduplication, and remediation issue creation.
- `backlog_groomer.py` owns backlog hygiene and repo-context-driven task generation, not operational incident synthesis.

Sprint selection is guided by three layers of context:
- `README.md` for the public product goal
- `STRATEGY.md` for sprint-to-sprint strategy memory
- `PLANNING_PRINCIPLES.md` for the stable north-star rubric the planner should optimize toward over time

Execution uses the same context model with different depth:
- high-level context: `README.md`, `STRATEGY.md`, `PLANNING_PRINCIPLES.md`
- evidence layers: `PRODUCTION_FEEDBACK.md` and `PLANNING_RESEARCH.md` when the task is strategic or evidence-driven
- low-level context: `CODEBASE.md`

## Fallbacks

DeepSeek has its own provider fallback: `openrouter → nanogpt → chutes`. It is kept last in the chain by default because it depends on extra provider configuration and should not consume retries when those providers are unavailable.

Before the queue dispatches DeepSeek, it now preflights the configured providers. For OpenRouter, the queue requires a readable `secrets.json` with a non-placeholder `openRouterApiKey`; if that credential is missing or invalid, DeepSeek is skipped in the agent chain and the task routes to the next configured fallback agent instead of spending an execution attempt on an authentication failure.

Strategic planning uses its own narrow fallback chain (`planner_agents`) so the control plane does not stall on a single Claude quota event and does not spray planning work across every model.

Repos can opt into bounded pre-planning research with `planning_research`. Before sprint selection, the planner refreshes `PLANNING_RESEARCH.md` only when it is older than `max_age_hours`; otherwise it reuses the existing artifact. Research is intentionally constrained to explicitly configured `https` URLs on allowed domains plus relative repo or repo-adjacent files. There is no search step and no open-ended browsing path.

Repos can also opt into bounded `production_feedback`. The first version supports explicit signal classes for `analytics`, `user_feedback`, `product_inspection`, and `incident_slo` (with legacy `planning_signals` config still accepted for backward compatibility). Before sprint selection, the planner refreshes `PRODUCTION_FEEDBACK.md` from configured web or file sources, plus any objective-derived business metrics, normalizes each entry with source, observed time, freshness, provenance, trust, privacy, and planning implications, and injects that artifact into strategic planning, backlog grooming, and evidence-heavy execution prompts.

Guardrails are explicit and inspectable. Each feedback entry carries `trust_level`, `privacy`, `trust_note`, and `privacy_note`, while repo config sets `minimum_trust_level`, `allowed_privacy_levels`, and `stale_after_hours`. Entries that are stale, too low-trust, or too privacy-sensitive remain visible in the artifact but are marked `Planning Use: guarded`, so they do not silently influence prioritization.

Repos can opt into bounded post-merge measurement with `outcome_attribution`. Issues attach one or more configured check IDs in an `## Outcome Checks` section, for example `- activation_rate`. When the task PR is opened and later merged, Agent OS records the task, issue, PR, and check IDs in `runtime/metrics/outcome_attribution.jsonl`. During planning and retrospectives, the planner refreshes due snapshots from the configured file or public-safe web sources, records a timestamped interpretation (`improved`, `unchanged`, `regressed`, or `inconclusive`), and surfaces that evidence alongside shipped work. External objective files can inject these checks automatically so merged work is evaluated against business metrics instead of repo-local completion proxies. If no measurable external metric is attached, the merge is still tracked explicitly as `inconclusive`.

## Objective Loop

Use this setup for a business-driven repo such as a private web app.

### 1. What To Create

You need 3 things:

1. `config.yaml`
2. `objectives/<repo>.yaml`
3. external evidence files in `~/.local/share/agent-os/evidence/<repo>/`

### 2. What Each File Does

`config.yaml`
- tells agent-os where repos are
- enables planning, production feedback, and outcome attribution
- points to the objectives directory and evidence directory

`objectives/<repo>.yaml`
- defines the business objective
- defines which metrics matter
- defines their weights
- defines where the evidence for those metrics lives
- defines how post-merge outcomes are judged

`evidence/<repo>/*.yaml`
- contains metric snapshots from GA4 or other external systems
- is written by your external exporter, not by agent-os

### 3. How To Define The Objective

In `objectives/<repo>.yaml`, define:

- `primary_outcome`
- `evaluation_window_days`
- `metrics`

Each metric should have:

- `id`
- `name`
- `weight`
- `direction`
- `source`
- `outcome_check`

For a web app, start with exactly these 4 metrics:

- `traffic`
- `conversion`
- `retention`
- `arpu`

### 4. How To Define Evidence

For each metric, create:

- one `*_latest.yaml` file for planning
- one `*_post_merge.yaml` file for post-merge outcome measurement

Example minimal set:

- `ga4_traffic_latest.yaml`
- `ga4_conversion_latest.yaml`
- `ga4_retention_latest.yaml`
- `ga4_arpu_latest.yaml`

Add these when you want closed-loop post-merge scoring:

- `ga4_traffic_post_merge.yaml`
- `ga4_conversion_post_merge.yaml`
- `ga4_retention_post_merge.yaml`
- `ga4_arpu_post_merge.yaml`

### 5. How Reward Works

Reward is the weighted business score implied by the objective file.

Example:

- traffic weight: `0.20`
- conversion weight: `0.35`
- retention weight: `0.25`
- arpu weight: `0.20`

If outcomes improve, the score goes positive. If outcomes stay flat, it stays near zero. If outcomes regress, it goes negative.

### 6. How Penalty Works

Penalty is not loss of autonomy. Penalty is negative value in the score.

Current interpretation mapping:

- `improved: 1.0`
- `unchanged: 0.0`
- `regressed: -1.0`
- `inconclusive: -0.35`

So penalty means:

- work shipped
- measured business outcomes did not improve, or got worse
- future planning and scoring should treat that direction as less valuable

### 7. Minimal First Version

Start with only:

- [example.config.yaml](/Users/kai/agent-os/example.config.yaml)
- [example.objective.yaml](/Users/kai/agent-os/example.objective.yaml)
- 4 GA4 evidence files for `traffic`, `conversion`, `retention`, and `arpu`

Ignore extra signals until the core loop is working.

### 8. Step By Step

1. Copy example.config.yaml to config.yaml
2. Copy example.objective.yaml to objectives/repo1.yaml
3. Edit the objective so the 4 metrics match your business
4. Write a small exporter that dumps GA4 snapshots into ~/.local/share/agent-os/evidence/repo1/
5. Enable production_feedback
6. Enable outcome_attribution
7. Let planner use *_latest.yaml
8. Let scorer use *_post_merge.yaml

## Example Config Snippets

```yaml
planning_research:
  enabled: true
  max_age_hours: 72
  max_sources: 4
  max_source_chars: 4000
  allowed_domains: [docs.example.com, competitor.example.com]
  artifact_file: PLANNING_RESEARCH.md
  sources:
    - name: Official docs
      type: web
      kind: official_docs
      url: https://docs.example.com/changelog
    - name: Competitor pricing
      type: web
      kind: competitor
      url: https://competitor.example.com/pricing
    - name: Launch notes
      type: file
      kind: repo_reference
      path: ../shared/launch-notes.md

production_feedback:
  enabled: true
  max_age_hours: 24
  stale_after_hours: 72
  minimum_trust_level: medium
  allowed_privacy_levels: [public, internal]
  max_inputs: 6
  max_source_chars: 4000
  allowed_domains: [analytics.example.com, community.example.com, status.example.com, app.example.com]
  artifact_file: PRODUCTION_FEEDBACK.md
  inputs:
    - name: Weekly activation funnel
      type: web
      signal_class: analytics
      url: https://analytics.example.com/public/weekly-funnel
      observed_at: 2026-03-19T08:00:00Z
      provenance: Public dashboard snapshot
      trust_level: high
      trust_note: Aggregated product analytics exported for planning
      privacy: public
      privacy_note: Aggregated public-safe metrics only

outcome_attribution:
  enabled: true
  max_source_chars: 4000
  allowed_domains: [analytics.example.com]
  checks:
    - id: activation_rate
      name: Activation rate
      type: web
      url: https://analytics.example.com/public/activation-after-release
      measurement_window_days: 7
      comparison_window: Compare 7 days after merge vs 7 days before merge
```

## Cron Reference

```cron
* * * * *   /path/to/agent-os/bin/run_autopull.sh      >> logs/autopull.log 2>&1
* * * * *   /path/to/agent-os/bin/run_dispatcher.sh  >> runtime/logs/dispatcher.log 2>&1
* * * * *   /path/to/agent-os/bin/run_queue.sh        >> runtime/logs/cron.log 2>&1
*/5 * * * * /path/to/agent-os/bin/run_pr_monitor.sh   >> runtime/logs/pr_monitor.log 2>&1
0 * * * *   /path/to/agent-os/bin/run_strategic_planner.sh >> runtime/logs/strategic_planner.log 2>&1
0 * * * *   /path/to/agent-os/bin/run_backlog_groomer.sh >> runtime/logs/backlog_groomer.log 2>&1
0 7 * * 1   /path/to/agent-os/bin/run_log_analyzer.sh >> runtime/logs/log_analyzer.log 2>&1
0 7 * * 1   /path/to/agent-os/bin/run_agent_scorer.sh >> runtime/logs/agent_scorer.log 2>&1
```

Each wrapper writes a timestamp banner to stderr before execution, so the log file for each cron job gets a clear per-run datetime marker automatically.

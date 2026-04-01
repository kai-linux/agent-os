Post-mortem for PR #98 CI debug cascade

Reviewed attempts
- `task-20260331-110613-fix-ci-failure-on-pr-98`
- `task-20260331-111114-fix-ci-failure-on-pr-98`
- `task-20260331-111518-follow-up-partial-debug-for-task-20260331-110613-f`
- `task-20260331-111916-follow-up-partial-debug-for-task-20260331-111114-f`
- `task-20260331-112615-fix-ci-failure-on-pr-98`
- `task-20260331-112817-follow-up-partial-debug-for-task-20260331-111916-f`
- `task-20260331-113117-follow-up-partial-debug-for-task-20260331-111518-f`
- `task-20260331-113316-follow-up-partial-debug-for-task-20260331-112615-f`

Root cause category: incorrect error classification

This was not a flaky-test incident and not an artifact-capture failure. The repeated follow-ups were caused by the PR-CI completion gate in `orchestrator/queue.py` rewriting otherwise successful debug runs to `partial` when the task body no longer contained the exact failed job names. The gate emits `CI_RERUN_REASON=missing_failed_job_context` whenever `_extract_ci_failed_checks()` cannot recover those names from markdown context. That turned a control-plane metadata loss into a new "fix CI on PR #98" loop.

Evidence
- The first two direct debug attempts produced concrete code changes and green local verification, but both queue logs end with `CI remediation completion gate downgraded task to partial (missing_failed_job_context)` instead of a remaining test failure.
- The next follow-up prompts stopped asking for the original CI fix and instead asked agents to "preserve the failing job name/check identifier" so the queue could validate the rerun.
- By `task-20260331-111916` and `task-20260331-111518`, the work had already shifted to repairing follow-up issue context propagation in `github_sync.py` and `github_dispatcher.py`, which confirms the system was debugging its own handoff contract rather than the PR's failing tests.
- `task-20260331-113316` finally concluded that no further code diff was needed because the branch already preserved failed-check context and the earlier follow-up fix was present.
- One branch of the cascade (`task-20260331-113117`) became impossible by construction: the prompt asked the agent to repair out-of-sandbox worktree permissions and rerun live GitHub Actions from an environment that explicitly could not do either. That was a downstream symptom of the misclassified partial state, not the original PR failure.

Why prior attempts were partial or blocked
- Successful branch changes were reclassified as unresolved CI work because the queue treated missing failed-job metadata as if the PR fix itself were incomplete.
- Partial-debug follow-ups inherited prose summaries, not durable structured failed-check metadata, so the same task kept respawning until the markdown context propagation bug was fixed.
- Once a follow-up issue focused on `missing_failed_job_context`, later prompts drifted into environment-only work such as repairing `index.lock` permissions or rerunning Actions, neither of which could permanently resolve the control-plane metadata defect.

Permanent fix
- Persist failed CI job names as structured task metadata/frontmatter for remediation tasks and follow-ups, and have `verify_pr_ci_debug_completion()` read that structured field instead of reparsing markdown issue bodies.

Rationale
- The existing design depends on formatter output and issue-body text surviving multiple handoffs unchanged.
- The cascade ended only after follow-up creation and parsing were patched to preserve those lines, which shows the true weak point is the transport format, not the CI signal.
- A structured `failed_checks` field would prevent formatter summarization, issue-template drift, and follow-up rewriting from erasing the verification target, so one successful fix attempt would stay attached to the original failing job instead of spawning another meta-debug task.

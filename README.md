# Agent OS

GitHub-native autonomous engineering loop for AI coding agents.

This system turns GitHub Issues + GitHub Project into the control plane, and uses a mailbox queue plus isolated git worktrees to let Codex, Claude, Gemini, and DeepSeek execute tasks recursively with fallbacks, escalation, and Telegram alerts.

---

## What this does

- Uses **GitHub Issues** as task objects
- Uses a **GitHub Project** as the kanban board / source of truth
- Dispatches `ready` issues into a local mailbox queue
- Runs AI coding agents in isolated **git worktrees**
- Supports **multi-model routing and fallback**
- Writes structured `.agent_result.md` handoff files
- Creates recursive follow-up tasks when needed
- Escalates gracefully instead of looping forever
- Sends status updates to Telegram
- Comments results back to GitHub issues and updates project status

---

## High-level architecture

```text
GitHub Issue
   ↓
GitHub Project (Status = Ready)
   ↓
github_dispatcher.py
   ↓
runtime/mailbox/inbox/*.md
   ↓
queue.py
   ↓
git worktree
   ↓
AI agent (Codex / Claude / Gemini / DeepSeek)
   ↓
.agent_result.md
   ↓
GitHub comment + project status sync + optional PR
   ↓
Done / Blocked / Escalated / Follow-up
```


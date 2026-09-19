"""Persist a scope baseline and manage dependent child work across workspaces."""

from __future__ import annotations

from orchestrator.delivery_contract import issue_source, register_issue
from orchestrator.delivery_store import (
    DeliveryConflict,
    DeliveryStore,
    goal_id,
    store_path,
)
from orchestrator.gh_project import gh, gh_json


def validate_plan(plan, allowed_repos, default_repo):
    children = plan.get("sub_issues", [])
    if not isinstance(children, list) or not 2 <= len(children) <= 20:
        raise ValueError(
            "A plan needs 2-20 work packages; larger programs need project-level decomposition"
        )
    keys = set()
    normalized = []
    for index, child in enumerate(children):
        if not isinstance(child, dict):
            raise ValueError("Every work package must be an object")
        key = str(child.get("key", index + 1))
        repo = str(child.get("repo") or default_repo)
        if key in keys or not child.get("title") or not child.get("body"):
            raise ValueError("Plan work packages need unique keys, titles and scope")
        if repo not in allowed_repos:
            raise ValueError(f"Unconfigured delivery workspace: {repo}")
        if child.get("kind", "task") not in {"program", "project", "milestone", "task"}:
            raise ValueError("Invalid work package kind")
        if not isinstance(child.get("depends_on", []), list):
            raise ValueError("Work package dependencies must be a list of keys")
        dependencies = [str(k) for k in child.get("depends_on", [])]
        normalized.append(
            {**child, "key": key, "repo": repo, "depends_on": dependencies}
        )
        keys.add(key)
    graph = {c["key"]: c["depends_on"] for c in normalized}
    visiting, visited = set(), set()

    def visit(key):
        if key not in graph or key in visiting:
            raise ValueError("Plan has a missing dependency or dependency cycle")
        if key in visited:
            return
        visiting.add(key)
        for dependency in graph[key]:
            visit(dependency)
        visiting.remove(key)
        visited.add(key)

    for key in graph:
        visit(key)
    return normalized


def manage_decomposition(cfg, repo, item, plan, project_key):
    mapping = {
        r["github_repo"]: (pk, r)
        for pk, p in cfg["github_projects"].items()
        for r in p.get("repos", [])
    }
    store = DeliveryStore(store_path(cfg))
    parent = register_issue(
        cfg,
        project_key,
        mapping[repo][1],
        item,
        "architecture",
        kind_override=plan.get("kind", "project"),
    )
    # Model-generated plans cannot widen the delivery target beyond the human
    # contract. Installed workspaces alone are not delegated scope.
    allowed = {repo} | (set(parent["contract"].get("targets", [])) & set(mapping))
    from orchestrator.reliability import enforced, tenant_for, execution_gate
    if enforced(cfg):
        execution_gate(cfg, parent)
        tenant = tenant_for(cfg, parent)
        allowed = {r for r in allowed if tenant_for(cfg, {"metadata": {"github_repo": r}}) == tenant}
    try:
        children = validate_plan(plan, allowed, repo)
    except ValueError as exc:
        store.wait(
            parent["id"],
            parent["revision"],
            "Plan needs correction before creating work: " + str(exc),
        )
        return []
    existing_plan = parent["metadata"].get("delivery_plan")
    if existing_plan is not None and existing_plan != children:
        raise DeliveryConflict(
            "Scope baseline already exists; use an explicit goal revision to replan"
        )
    store.bind_execution(parent["id"], parent["revision"], {"delivery_plan": children})
    created = {}
    for child in children:
        marker = (
            f"<!-- agent-os-plan:{parent['id']}:{parent['revision']}:{child['key']} -->"
        )
        matches = (
            gh_json(
                [
                    "issue",
                    "list",
                    "-R",
                    child["repo"],
                    "--state",
                    "all",
                    "--search",
                    marker,
                    "--limit",
                    "100",
                    "--json",
                    "number,title,body,url,state",
                ]
            )
            or []
        )
        found = next((i for i in matches if marker in i.get("body", "")), None)
        parent_ref = f"{repo}#{item['number']}"
        body = child["body"] + f"\n\nPart of {parent_ref}\n\n{marker}"
        if found is None:
            url = gh(
                [
                    "issue",
                    "create",
                    "-R",
                    child["repo"],
                    "--title",
                    child["title"],
                    "--body",
                    body,
                ]
            )
            found = {
                "number": int(url.rsplit("/", 1)[-1]),
                "title": child["title"],
                "body": body,
                "url": url,
                "state": "OPEN",
            }
        pk, rcfg = mapping[child["repo"]]
        kind = child.get("kind", "task")
        goal = register_issue(
            cfg,
            pk,
            rcfg,
            found,
            child.get("task_type", "implementation"),
            parent_id=parent["id"],
            ready=True,
            kind_override=kind,
        )
        store.bind_execution(
            goal["id"],
            goal["revision"],
            {"intent_source": "proposed_work_package", "plan_key": child["key"]},
        )
        created[child["key"]] = (goal, {**found, "repo": child["repo"]})
        pcfg = cfg["github_projects"][pk]
        gh(
            [
                "project",
                "item-add",
                str(pcfg["project_number"]),
                "--owner",
                cfg["github_owner"],
                "--url",
                found["url"],
            ]
        )
    for child in children:
        for dependency in child["depends_on"]:
            store.depend(created[child["key"]][0]["id"], created[dependency][0]["id"])
        if child["depends_on"]:
            g = created[child["key"]][0]
            if store.get(g["id"])["state"] == "ready":
                store.wait(
                    g["id"],
                    g["revision"],
                    "dependency: waiting for verified prerequisite work",
                )
    # All work remains owned by the open parent. State projection and dispatch
    # use the graph; splitting a program is never reported as delivery.
    store.bind_execution(parent["id"], parent["revision"], {"plan_materialized": True})
    return [created[c["key"]][1] for c in children if not c["depends_on"]]


def qualified_plan(cfg, parent):
    """Enforced planning uses the same ownership, trace and billing boundary as work."""
    import json
    from orchestrator.delivery import begin_worker, finish_worker
    from orchestrator.model_gateway import call_model
    from orchestrator.reliability_store import ReliabilityStore
    from orchestrator.task_decomposer import DECOMPOSE_PROMPT
    records = ReliabilityStore(cfg)
    adapter = cfg.get("reliability", {}).get("planning_model_adapter")
    if not adapter:
        records.delivery.wait(parent["id"], parent["revision"], "Configure a tenant-scoped planning model adapter; unmetered CLI planning is disabled")
        return None
    meta = {"goal_id": parent["id"], "goal_revision": parent["revision"],
            "task_id": "plan-" + parent["id"], "branch": "planning"}
    key = None
    try:
        key = begin_worker(cfg, meta, "planner", adapter, 3, parent["metadata"]["workspace"])
        response = call_model(cfg, key, adapter, {"prompt": DECOMPOSE_PROMPT.format(title=parent["title"], body=parent["original"])})
        plan = response["output"]
        if isinstance(plan, str):
            plan = json.loads(plan)
        if not isinstance(plan, dict) or plan.get("type") != "epic":
            raise ValueError("A program needs structured work packages")
        finish_worker(cfg, meta, key, {"status": "complete"})
        records.seal_usage(key, [response["receipt_key"]], actor="planning-adapter")
        return plan
    except Exception as exc:
        if key:
            finish_worker(cfg, meta, key, {"status": "blocked", "blocker_code": "planning_failed"})
        records.delivery.wait(parent["id"], parent["revision"], "Planning requires reconciliation: " + type(exc).__name__)
        return None


def dispatchable(cfg, repo, number):
    """Consult durable ownership before either board or label based dispatch."""
    if not cfg.get("root_dir"):
        return True
    store = DeliveryStore(store_path(cfg))
    try:
        goal = store.get(goal_id(issue_source(repo, number)))
    except DeliveryConflict:
        return True
    if goal["metadata"].get("plan_materialized"):
        return False
    if goal["state"] != "ready" or goal["metadata"].get("mailbox_payload"):
        return False
    if not store.execution_allowed(goal["id"], goal["revision"]):
        return False
    snapshot = store.snapshot()
    by_id = {g["id"]: g for g in snapshot["goals"]}
    dependencies = [
        d["requires_id"] for d in snapshot["dependencies"] if d["goal_id"] == goal["id"]
    ]
    return all(by_id[k]["state"] == "succeeded" for k in dependencies)

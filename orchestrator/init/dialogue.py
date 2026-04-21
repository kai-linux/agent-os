from __future__ import annotations

from orchestrator.init import ui


DEFAULT_IDEA = "A useful software product with one clear core workflow."
DEFAULT_SUCCESS = "The primary user is the project owner. Success means a usable first version exists and is easy to iterate on."

KIND_OPTIONS = {
    "1": "web",
    "2": "mobile",
    "3": "game",
    "4": "api",
    "5": "cli",
    "6": "desktop",
    "7": "other",
}


def run(existing: dict | None = None) -> dict[str, str]:
    if existing:
        return existing

    idea = ui.prompt(
        "What do you want to build? Leave blank if you want the agent to help shape it.",
        default=DEFAULT_IDEA,
    )

    print("What kind of thing is it?")
    print("  [1] web app      [2] mobile app   [3] game")
    print("  [4] API          [5] CLI          [6] desktop app")
    print("  [7] not sure yet / other")
    kind_choice = ui.choice("", ["1", "2", "3", "4", "5", "6", "7"], default="7")
    kind = KIND_OPTIONS[kind_choice]

    stack_preference = ui.prompt('Any stack preference? ("auto" lets the agent decide)', default="auto")
    success = ui.prompt(
        "Who is the user, and what does success look like in one sentence? Leave blank to use a sensible default.",
        default=DEFAULT_SUCCESS,
    )

    return {
        "idea": idea,
        "kind": kind,
        "stack_preference": stack_preference or "auto",
        "success_criteria": success,
    }

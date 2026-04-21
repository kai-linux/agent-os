from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from orchestrator.init import ui
from orchestrator.init.state import State, utc_now_iso


TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{30,}$")


class TelegramError(RuntimeError):
    pass


def validate_token_shape(token: str) -> bool:
    return bool(TOKEN_RE.fullmatch(token.strip()))


def mask_token(token: str) -> str:
    trimmed = token.strip()
    if len(trimmed) <= 4:
        return "****"
    return f"{trimmed[:6]}...{trimmed[-4:]}"


def _api_request(token: str, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = None
    headers = {}
    if payload is not None:
        encoded = urllib.parse.urlencode(payload).encode("utf-8")
        data = encoded
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        raise TelegramError(text) from exc
    except urllib.error.URLError as exc:
        raise TelegramError(str(exc.reason)) from exc
    payload = json.loads(body)
    if not payload.get("ok"):
        raise TelegramError(payload.get("description", "Telegram API request failed"))
    return payload["result"]


def _prompt_token(existing_username: str | None = None) -> str:
    while True:
        label = "Bot token" if not existing_username else f"Bot token for @{existing_username}"
        token = ui.password(label)
        if validate_token_shape(token):
            return token
        ui.warn("That does not look like a Telegram bot token.")


def _choose_chat(chats: list[dict[str, str]]) -> dict[str, str]:
    if len(chats) == 1:
        return chats[0]
    print("Multiple chats found:")
    for idx, chat in enumerate(chats, start=1):
        print(f"  [{idx}] {chat['label']} (id={chat['id']})")
    while True:
        choice = ui.prompt("")
        if choice.isdigit() and 1 <= int(choice) <= len(chats):
            return chats[int(choice) - 1]
        ui.warn("Choose one of the listed chats.")


def run(state: State, repo_full_name: str, *, dry_run: bool = False) -> dict[str, str]:
    existing = state.get("telegram", {})
    if existing.get("chat_id"):
        print(f"  [1] Reuse @{existing.get('bot_username', 'bot')}  [2] Pair a different bot/chat")
        reuse = ui.choice("", ["1", "2"], default="1")
        if reuse == "1":
            token = _prompt_token(existing.get("bot_username"))
            return {
                "token": token,
                "chat_id": str(existing["chat_id"]),
                "bot_username": existing.get("bot_username", ""),
            }

    if dry_run:
        data = {"token": "123456:dryrun-token-placeholder-abcdefghijklmnopqrstuvwxyz", "chat_id": "123456789", "bot_username": "agentos_dryrun_bot"}
        state.mark("telegram", {"bot_username": data["bot_username"], "chat_id": data["chat_id"], "verified_at": utc_now_iso()})
        return data

    print("Agent OS uses Telegram for the daily digest, escalations, and /on /off /status commands.")
    print("1. Open Telegram, message @BotFather, send /newbot, and create a bot.")
    token = _prompt_token()
    ui.info(f"Verifying token {mask_token(token)}...")
    me = _api_request(token, "getMe")
    username = me["username"]
    ui.ok(f"Token valid: @{username}")
    print(f"\n2. Open a chat with @{username} and send /start (or any message).")
    input("Press Enter when you've sent the message. ")
    ui.info("Polling getUpdates...")

    updates = _api_request(token, "getUpdates", {"timeout": 30})
    chats: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for entry in updates:
        message = entry.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", "")).strip()
        if not chat_id or chat_id in seen_ids:
            continue
        seen_ids.add(chat_id)
        label = chat.get("title") or chat.get("first_name") or chat.get("username") or chat_id
        chats.append({"id": chat_id, "label": label})
    if not chats:
        raise TelegramError("No messages found for the bot. Send /start to the bot and re-run init.")

    selected = _choose_chat(chats)
    _api_request(
        token,
        "sendMessage",
        {
            "chat_id": selected["id"],
            "text": f"✅ Agent OS control plane linked to {repo_full_name}. You can now /on /off /status from this chat.",
        },
    )
    state.mark(
        "telegram",
        {
            "bot_username": username,
            "chat_id": selected["id"],
            "verified_at": utc_now_iso(),
        },
    )
    return {"token": token, "chat_id": selected["id"], "bot_username": username}


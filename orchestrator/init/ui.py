from __future__ import annotations

import getpass
import sys


_USE_COLOR = sys.stdout.isatty()


def _color(code: str, text: str) -> str:
    if not _USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def _emit(symbol: str, message: str, color_code: str) -> None:
    print(f"{_color(color_code, symbol)} {message}")


def header(title: str) -> None:
    print(f"\n═══ {title} ═══\n")


def info(message: str) -> None:
    _emit("▸", message, "36")


def ok(message: str) -> None:
    _emit("✓", message, "32")


def warn(message: str) -> None:
    _emit("!", message, "33")


def fail(message: str) -> None:
    _emit("✗", message, "31")


def prompt(message: str, *, default: str | None = None) -> str:
    print(message)
    suffix = "> " if default is None else f"> [{default}] "
    value = input(suffix).strip()
    if not value and default is not None:
        return default
    return value


def choice(message: str, allowed: list[str], *, default: str | None = None) -> str:
    normalized = {item.lower(): item for item in allowed}
    while True:
        value = prompt(message, default=default)
        lowered = value.lower()
        if lowered in normalized:
            return normalized[lowered]
        warn(f"Choose one of: {', '.join(allowed)}")


def password(message: str) -> str:
    while True:
        value = getpass.getpass(f"{message}: ").strip()
        if value:
            return value
        warn("Value cannot be empty.")


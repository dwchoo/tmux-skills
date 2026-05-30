from __future__ import annotations

import re


ANSI_RE = re.compile(
    r"(?:\x1B\][^\x07\x1B]*(?:\x07|\x1B\\))"  # OSC: titles, hyperlinks, etc.
    r"|(?:\x1B[P^_].*?\x1B\\)"  # DCS/PM/APC string controls.
    r"|(?:\x1B\[[0-?]*[ -/]*[@-~])"  # CSI controls, including colors/cursor movement.
    r"|(?:\x1B[@-Z\\-_])",  # 7-bit C1/Fe controls.
    re.DOTALL,
)
PROMPT_RE = re.compile("(?:[$#%]|\\u276f|\\u276e|\\u279c|\\u03bb)\\s*$")


def strip_ansi(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return ANSI_RE.sub("", normalized)


def prompt_like(output: str) -> bool:
    for line in reversed(strip_ansi(output).splitlines()):
        stripped = line.strip()
        if stripped:
            return bool(PROMPT_RE.search(stripped))
    return False

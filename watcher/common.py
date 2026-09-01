"""Shared event shape and tool-call extraction for transcript parsers.

Both the Claude Code and Codex parsers reduce a raw transcript to the same
compact event stream: user prompts, assistant replies, and structured tool
facts (files touched, commands run). High-volume, low-signal material -- tool
result bodies, thinking blocks, attachments -- is dropped here, before anything
is sent or even redacted, which is where the token savings come from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

FILE_TOOLS = {"Read", "Edit", "Write", "NotebookEdit", "MultiEdit"}
COMMAND_TOOLS = {"Bash", "BashOutput"}


@dataclass
class ParsedEvent:
    seq: int
    role: str
    kind: str
    content: str = ""
    files: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    is_sidechain: bool = False
    ts: str | None = None

    def is_empty(self) -> bool:
        return not (self.content.strip() or self.files or self.commands)


@dataclass
class ParsedSession:
    session_id: str
    tool: str
    cwd: str | None = None
    git_branch: str | None = None
    ai_title: str | None = None
    last_prompt: str | None = None
    started_at: str | None = None
    is_sidechain: bool = False


def summarize_tool_use(name: str, tool_input: dict[str, Any]) -> ParsedEvent | None:
    """Turn one tool_use block into a compact fact, or None to drop it."""
    files: list[str] = []
    commands: list[str] = []
    content = ""

    if name in FILE_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        if path:
            files.append(str(path))
            content = f"{name} {path}"
    elif name in COMMAND_TOOLS:
        command = tool_input.get("command")
        if command:
            command = " ".join(str(command).split())
            commands.append(command[:400])
            content = f"$ {command[:400]}"
    elif name in {"WebFetch", "WebSearch"}:
        target = tool_input.get("url") or tool_input.get("query")
        if target:
            content = f"{name}: {str(target)[:200]}"
    elif name == "Agent":
        desc = tool_input.get("description") or tool_input.get("subagent_type")
        if desc:
            content = f"Delegated to sub-agent: {str(desc)[:200]}"
    else:
        content = f"{name} called"

    if not (content or files or commands):
        return None
    return ParsedEvent(seq=-1, role="assistant", kind="tool_use",
                       content=content, files=files, commands=commands)

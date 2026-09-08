"""Deterministic project identity.

``project_key`` is the hard partition that keeps unrelated work apart. It must
be derived identically on every device, so this module is stdlib-only and is
imported by the hub, the watcher, and the MCP wrapper alike.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

GLOBAL_PROJECT = "__global__"

_SSH_REMOTE = re.compile(r"^(?:ssh://)?(?:[^@/]+@)?([^:/]+)[:/](.+?)(?:\.git)?/?$")
_URL_REMOTE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+?)(?:\.git)?/?$")


def normalize_remote(remote: str) -> str | None:
    """Reduce any git remote form to ``host/owner/repo``.

    git@github.com:o/r.git, https://github.com/o/r.git and ssh://git@github.com/o/r
    all collapse to github.com/o/r, so the same repo cloned over different
    protocols on different machines yields one project_key.
    """
    remote = (remote or "").strip()
    if not remote:
        return None

    match = _URL_REMOTE.match(remote) or _SSH_REMOTE.match(remote)
    if not match:
        return None
    host, path = match.group(1).lower(), match.group(2).strip("/")
    if not host or not path:
        return None
    return f"{host}/{path}".lower()


def find_repo_root(start: str | os.PathLike[str]) -> Path | None:
    """Walk up looking for a .git entry. Handles worktrees (.git as a file)."""
    try:
        current = Path(start).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    if current.is_file():
        current = current.parent
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def _git_remote(repo_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def git_branch(repo_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def derive_project(cwd: str | os.PathLike[str] | None) -> dict[str, str | None]:
    """Resolve a working directory to stable project identity.

    Preference order: git remote (stable across machines and clone paths),
    then repo root path, then the raw directory. Returns project_key,
    display_name, repo_root and git_remote.
    """
    if not cwd:
        return {
            "project_key": GLOBAL_PROJECT,
            "display_name": "global",
            "repo_root": None,
            "git_remote": None,
        }

    raw = Path(str(cwd)).expanduser()
    repo_root = find_repo_root(raw)

    if repo_root is not None:
        remote = _git_remote(repo_root)
        normalized = normalize_remote(remote) if remote else None
        if normalized:
            return {
                "project_key": normalized,
                "display_name": normalized.rsplit("/", 1)[-1],
                "repo_root": str(repo_root),
                "git_remote": remote,
            }
        return {
            "project_key": f"path:{repo_root}",
            "display_name": repo_root.name,
            "repo_root": str(repo_root),
            "git_remote": None,
        }

    try:
        resolved = raw.resolve()
    except (OSError, RuntimeError):
        resolved = raw
    return {
        "project_key": f"path:{resolved}",
        "display_name": resolved.name or str(resolved),
        "repo_root": None,
        "git_remote": None,
    }


# Directory names carry scaffolding words that bury the distinctive part of the
# name. Measured against the live hub: "RedLamb-build" scores 0.20 against the
# sessions that built RedLamb, "RedLamb" scores 1.00 -- splitting the separator
# is what makes the difference.
_GENERIC_NAME_TOKENS = frozenset({
    "bin", "build", "builds", "code", "debug", "dist", "final", "master", "new",
    "obj", "old", "out", "output", "project", "projects", "release", "repo",
    "repos", "source", "sources", "src", "target", "temp", "test", "tests",
    "tmp", "workspace",
})


def search_seed(project_key: str) -> str:
    """The distinctive part of a project_key, as a query the index can match.

    Used to find sibling projects when a brief comes back empty. Handles both
    key forms -- ``path:/Volumes/T7 Shield/T7 shield/RedLamb-build`` and
    ``github.com/owner/repo`` -- and both path separators, since keys are
    minted on whichever machine did the work.
    """
    name = project_key.split(":", 1)[1] if project_key.startswith("path:") else project_key
    name = name.replace("\\", "/").rstrip("/")
    base = name.rsplit("/", 1)[-1]
    parts = [p for p in re.split(r"[-_.\s]+", base) if p]
    kept = [p for p in parts if p.lower() not in _GENERIC_NAME_TOKENS]
    return " ".join(kept or parts)

"""Git-операции обновления модуля. Без shell=True, без сети в тестах."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

__all__ = ["ModuleGitError", "apply_module_update", "check_module_update"]

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_DEFAULT_DIR = "/app/mia/modules"
_NET_TIMEOUT = 15.0


class ModuleGitError(Exception):
    """code + human — в RPC, без traceback клиенту."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.human = message
        super().__init__(message)


def check_module_update(name: str) -> dict[str, Any]:
    repo = _repo(name)
    current_sha = _git(repo, "rev-parse", "HEAD", timeout=5.0).strip()
    remote_sha = _remote_head(repo)
    tags = _semver_tags(repo)
    return {
        "name": name,
        "current_sha": current_sha,
        "remote_sha": remote_sha,
        "current_label": _label(current_sha, tags),
        "remote_label": _label(remote_sha, tags),
        "update_available": current_sha != remote_sha,
    }


def apply_module_update(name: str) -> dict[str, Any]:
    info = check_module_update(name)
    repo = _repo(name)
    _git(repo, "fetch", "origin", timeout=45.0)
    _git(repo, "reset", "--hard", info["remote_sha"], timeout=10.0)
    sha = _git(repo, "rev-parse", "HEAD", timeout=5.0).strip()
    return {"name": name, "sha": sha, "version": info["remote_label"]}


def _repo(name: str) -> Path:
    if not _NAME.fullmatch(name):
        raise ModuleGitError("VALIDATION", "Invalid request")
    root = Path(os.environ.get("BELLE_MODULES_DIR") or _DEFAULT_DIR).resolve()
    path = (root / name).resolve()
    if path.parent != root:
        raise ModuleGitError("VALIDATION", "Invalid request")
    if not (path / ".git").exists():
        raise ModuleGitError("NO_GIT", "Module is not a git repository")
    return path


def _remote_head(repo: Path) -> str:
    sha = _first_sha(_git(repo, "ls-remote", "origin", "HEAD"))
    if sha:
        return sha
    sha = _first_sha(_git(repo, "ls-remote", "origin", "refs/heads/main"))
    if sha:
        return sha
    raise ModuleGitError("GIT_FAILED", "Could not read remote HEAD")


def _semver_tags(repo: Path) -> list[tuple[tuple[int, int, int], str, str]]:
    found: list[tuple[tuple[int, int, int], str, str]] = []
    for sha, ref in _ls_remote_lines(_git(repo, "ls-remote", "--tags", "--refs", "origin")):
        if not ref.startswith("refs/tags/"):
            continue
        tag = ref[10:]
        match = _SEMVER.fullmatch(tag)
        if match is None:
            continue
        key = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        found.append((key, tag, sha))
    found.sort(key=lambda item: item[0], reverse=True)
    return found


def _label(sha: str, tags: list[tuple[tuple[int, int, int], str, str]]) -> str:
    # Только max semver: если он на этом SHA — тег, иначе short SHA.
    if tags and tags[0][2] == sha:
        return tags[0][1]
    return sha[:7]


def _ls_remote_lines(out: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0]:
            rows.append((parts[0], parts[1]))
    return rows


def _first_sha(out: str) -> str:
    rows = _ls_remote_lines(out)
    return rows[0][0] if rows else ""


def _git(repo: Path, *args: str, timeout: float = _NET_TIMEOUT) -> str:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise ModuleGitError("GIT_FAILED", "Git command timed out") from exc
    except OSError as exc:
        raise ModuleGitError("GIT_FAILED", "Git command failed") from exc
    if completed.returncode != 0:
        raise ModuleGitError("GIT_FAILED", "Git command failed")
    return completed.stdout

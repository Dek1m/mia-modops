"""check/update модуля: mock subprocess, без сети."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from modules.modops.module_git import (
    ModuleGitError,
    apply_module_update,
    check_module_update,
)
from modules.modops.provider import ModopsProvider

LOCAL = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
REMOTE = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
TAG = "v1.2.3"


def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "modules"
    repo = root / "fs"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    monkeypatch.setenv("BELLE_MODULES_DIR", str(root))
    return repo


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["git"], returncode=returncode, stdout=stdout, stderr="")


def _fake_run(mapping: dict[tuple[str, ...], subprocess.CompletedProcess[str]]):
    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert kwargs.get("shell") is False
        key = tuple(args[1:])
        if key not in mapping:
            raise AssertionError(f"unexpected git {args}")
        return mapping[key]
    return run


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return _repo(tmp_path, monkeypatch)


def test_check_update_available(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "run", _fake_run({
        ("rev-parse", "HEAD"): _completed(LOCAL + "\n"),
        ("ls-remote", "origin", "HEAD"): _completed(f"{REMOTE}\tHEAD\n"),
        ("ls-remote", "--tags", "--refs", "origin"): _completed(
            f"{REMOTE}\trefs/tags/{TAG}\n",
        ),
    }))
    result = check_module_update("fs")
    assert result["update_available"] is True
    assert result["current_sha"] == LOCAL
    assert result["remote_sha"] == REMOTE
    assert result["current_label"] == LOCAL[:7]
    assert result["remote_label"] == TAG
    assert result["name"] == "fs"


def test_check_update_already_current(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "run", _fake_run({
        ("rev-parse", "HEAD"): _completed(LOCAL + "\n"),
        ("ls-remote", "origin", "HEAD"): _completed(f"{LOCAL}\tHEAD\n"),
        ("ls-remote", "--tags", "--refs", "origin"): _completed(
            f"{LOCAL}\trefs/tags/{TAG}\n",
        ),
    }))
    result = check_module_update("fs")
    assert result["update_available"] is False
    assert result["current_label"] == TAG
    assert result["remote_label"] == TAG


def test_check_update_no_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "modules"
    (root / "fs").mkdir(parents=True)
    monkeypatch.setenv("BELLE_MODULES_DIR", str(root))
    with pytest.raises(ModuleGitError) as exc:
        check_module_update("fs")
    assert exc.value.code == "NO_GIT"


def test_check_update_git_failed(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "run", _fake_run({
        ("rev-parse", "HEAD"): _completed(returncode=1),
    }))
    with pytest.raises(ModuleGitError) as exc:
        check_module_update("fs")
    assert exc.value.code == "GIT_FAILED"


def test_apply_update_resets_to_remote(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert kwargs.get("shell") is False
        key = tuple(args[1:])
        calls.append(key)
        mapping = {
            ("rev-parse", "HEAD"): _completed(REMOTE + "\n"),
            ("ls-remote", "origin", "HEAD"): _completed(f"{REMOTE}\tHEAD\n"),
            ("ls-remote", "--tags", "--refs", "origin"): _completed(
                f"{REMOTE}\trefs/tags/{TAG}\n",
            ),
            ("fetch", "origin"): _completed(),
            ("reset", "--hard", REMOTE): _completed(),
        }
        if key not in mapping:
            if key == ("rev-parse", "HEAD"):
                return _completed(REMOTE + "\n")
            raise AssertionError(f"unexpected git {args}")
        return mapping[key]

    monkeypatch.setattr(subprocess, "run", run)
    result = apply_module_update("fs")
    assert ("fetch", "origin") in calls
    assert ("reset", "--hard", REMOTE) in calls
    assert result == {"name": "fs", "sha": REMOTE, "version": TAG}


@pytest.mark.asyncio
async def test_provider_check_update_maps_error(
    provider: ModopsProvider, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(_name: str) -> dict[str, Any]:
        raise ModuleGitError("NO_GIT", "Module is not a git repository")

    monkeypatch.setattr("modules.modops.provider.check_module_update", boom)
    with pytest.raises(Exception) as exc:
        await provider.check_update("fs")
    assert exc.value.code == "NO_GIT"
    assert exc.value.human == "Module is not a git repository"

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_git_environment(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Trail Fixture Author")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "author@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Trail Fixture Committer")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "committer@example.invalid")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    for variable in (
        "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_COUNT",
        "GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE", "GIT_EXTERNAL_DIFF", "GIT_PAGER",
    ):
        monkeypatch.delenv(variable, raising=False)


class LocalRepo:
    def __init__(self, root: Path):
        self.root = root
        self.commit_count = 0
        self.root.mkdir()
        self.git("init", "--initial-branch=main", "--quiet")

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                "git", "--no-pager", "--no-optional-locks", "-c", "commit.gpgsign=false",
                "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                "-C", str(self.root), *args,
            ],
            check=check, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def write(self, path: str, content: str | bytes) -> Path:
        destination = self.root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
        return destination

    def commit(self, message: str) -> str:
        self.commit_count += 1
        date = f"2001-01-01T00:{self.commit_count // 60:02}:{self.commit_count % 60:02}+00:00"
        self.git("add", "--all", "--", ".")
        self.git("commit", "--quiet", "--date", date, "-m", message)
        return self.git("rev-parse", "HEAD").stdout.decode().strip()

    def state(self) -> dict:
        files = {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file() and ".git" not in path.relative_to(self.root).parts
        }
        index = self.root / ".git" / "index"
        return {
            "head": (self.root / ".git" / "HEAD").read_bytes(),
            "refs": self.git("show-ref", "--heads", "--tags").stdout,
            "index": index.read_bytes() if index.exists() else None,
            "config": (self.root / ".git" / "config").read_bytes(),
            "status": self.git("status", "--porcelain=v1", "-z").stdout,
            "files": files,
        }


@pytest.fixture
def repo(tmp_path) -> LocalRepo:
    return LocalRepo(tmp_path / "repository with spaces")


@dataclass(frozen=True)
class RangeHistory:
    repo: LocalRepo
    path: str
    origin: str
    edit: str
    shift: str
    unrelated: str
    insert: str
    delete: str

    @property
    def relevant(self) -> list[str]:
        return [self.delete, self.insert, self.edit, self.origin]


@pytest.fixture
def range_history(repo) -> RangeHistory:
    path = "src/player.py"
    repo.write(path, "header\n\ndef target():\n    a = 1\n    b = 2\n    return a + b\n\n# footer\n")
    origin = repo.commit("Introduce target")
    repo.write(path, "header\n\ndef target():\n    a = 7\n    b = 2\n    return a + b\n\n# footer\n")
    edit = repo.commit("Change target")
    repo.write(path, "extra\nheader\n\ndef target():\n    a = 7\n    b = 2\n    return a + b\n\n# footer\n")
    shift = repo.commit("Insert unrelated prefix")
    repo.write(path, "extra\nheader\n\ndef target():\n    a = 7\n    b = 2\n    return a + b\n\n# footer changed\n")
    repo.write("unrelated.txt", "Different file\n")
    unrelated = repo.commit("Change unrelated footer and file")
    repo.write(path, "extra\nheader\n\ndef target():\n    a = 7\n    b = 2\n    c = 3\n    return a + b\n\n# footer changed\n")
    insert = repo.commit("Insert within target")
    repo.write(path, "extra\nheader\n\ndef target():\n    a = 7\n    c = 3\n    return a + b\n\n# footer changed\n")
    delete = repo.commit("Delete within target")
    return RangeHistory(repo, path, origin, edit, shift, unrelated, insert, delete)

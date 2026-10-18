from __future__ import annotations

import os
import re
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath


class TrailError(Exception):
    """An input or repository error that can be shown to the user."""


def source_lines(text: str) -> list[str]:
    """Split source at Git's LF line boundaries."""
    if not text:
        return []
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
    return lines


def display_source(text: str) -> str:
    """Show non-LF control characters without creating extra source rows."""
    visible = {code: chr(0x2400 + code) for code in range(32) if code not in {9, 10}}
    visible.update({0x85: "␤", 0x2028: "␤", 0x2029: "¶", 0x7F: "␡"})
    return text.replace("\r\n", "\n").translate(visible)


@dataclass(frozen=True)
class LineRange:
    start: int
    end: int
    label: str = ""

    def validate(self, line_count: int) -> None:
        if not 1 <= self.start <= self.end <= line_count:
            raise TrailError(
                f"Select lines between 1 and {line_count}; received {self.start}:{self.end}."
            )


@dataclass(frozen=True)
class Snapshot:
    path: str
    commit: str
    text: str
    working_tree_changed: bool

    @property
    def lines(self) -> list[str]:
        return source_lines(self.text)


@dataclass(frozen=True)
class HistoryEntry:
    commit: str
    parents: tuple[str, ...]
    author: str
    authored_at: str
    committed_at: str
    subject: str
    patch: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Timeline:
    repository: str
    path: str
    commit: str
    selection: LineRange
    entries: tuple[HistoryEntry, ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "repository": self.repository,
            "path": self.path,
            "starting_commit": self.commit,
            "selection": asdict(self.selection),
            "warnings": list(self.warnings),
            "entries": [entry.to_dict() for entry in self.entries],
        }


class Repository:
    def __init__(self, location: str | Path, ref: str = "HEAD"):
        location = Path(location).expanduser().resolve()
        if not location.is_dir():
            raise TrailError(f"Repository directory does not exist: {location}")
        self.root = location
        root = os.fsdecode(self._git("rev-parse", "--show-toplevel")).removesuffix("\n")
        self.root = Path(root)
        self.commit = self._git(
            "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"
        ).decode().strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", self.commit):
            raise TrailError(f"Could not resolve a commit from {ref!r}.")

    def _git(self, *args: str, timeout: int = 120) -> bytes:
        command = [
            "git",
            "--no-pager",
            "--literal-pathspecs",
            "-c", "color.ui=false",
            "-c", "core.quotepath=false",
            "-c", "core.fsmonitor=false",
            "-c", "diff.autoRefreshIndex=false",
            "-C", str(self.root),
            *args,
        ]
        try:
            environment = os.environ.copy()
            environment.update({
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_NO_LAZY_FETCH": "1",
                "GIT_TERMINAL_PROMPT": "0",
            })
            result = subprocess.run(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=timeout, env=environment,
            )
        except FileNotFoundError as error:
            raise TrailError("Git is not installed or is not on PATH.") from error
        except subprocess.TimeoutExpired as error:
            raise TrailError("Git timed out. Try a smaller selection or a newer starting ref.") from error
        if result.returncode:
            message = result.stderr.decode("utf-8", errors="replace").strip()
            raise TrailError(message or f"Git exited with status {result.returncode}.")
        return result.stdout

    def path(self, value: str | Path) -> str:
        value = str(value)
        if any(character in value for character in "\0\n\r"):
            raise TrailError("Use a file path without NUL or newline characters.")
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            try:
                candidate = candidate.parent.resolve() / candidate.name
                value = str(candidate.relative_to(self.root))
            except ValueError as error:
                raise TrailError("Select a file inside this repository.") from error
        normalized = PurePosixPath(value)
        if (
            not value
            or normalized.is_absolute()
            or ".." in normalized.parts
            or normalized.as_posix() == "."
            or any(character in value for character in "\0\n\r")
        ):
            raise TrailError("Use a repository-relative file path without parent traversal or newlines.")
        return normalized.as_posix()

    def files(self) -> tuple[str, ...]:
        output = self._git("ls-tree", "-r", "--name-only", "-z", self.commit)
        return tuple(
            item.decode("utf-8", errors="replace")
            for item in output.split(b"\0")
            if item
        )

    def snapshot(self, path: str | Path) -> Snapshot:
        path = self.path(path)
        raw = self._git("show", f"{self.commit}:{path}")
        if b"\0" in raw:
            raise TrailError(f"{path} is a binary file. Select a text file.")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise TrailError(f"{path} is not UTF-8 text. Select a UTF-8 source file.") from error
        if not source_lines(text):
            raise TrailError(f"{path} is empty at the selected commit.")
        changed = bool(
            self._git(
                "diff", "--no-ext-diff", "--no-textconv", "--name-only",
                self.commit, "--", path,
            ).strip()
        )
        return Snapshot(path, self.commit, text, changed)

    def history(self, snapshot: Snapshot, selection: LineRange) -> Timeline:
        if snapshot.commit != self.commit or snapshot.path != self.path(snapshot.path):
            raise TrailError("This selection belongs to a different repository snapshot.")
        selection.validate(len(snapshot.lines))
        format_string = "%x00%H%x00%P%x00%an%x00%aI%x00%cI%x00%s%x00"
        output = self._git(
            "log", "--no-ext-diff", "--no-textconv", "--no-color",
            "--no-show-signature", "--root", "--find-renames",
            f"--format={format_string}",
            f"-L{selection.start},{selection.end}:./{snapshot.path}",
            self.commit, "--",
        )
        entries = parse_history(output)
        warnings = [
            "Textual range history from one committed snapshot. Moves between files and "
            "renamed symbols are not guaranteed.",
            "Merge history follows Git's range traversal; this is not every parent path.",
        ]
        shallow = self._git("rev-parse", "--is-shallow-repository").decode().strip() == "true"
        if shallow:
            warnings.append("This is a shallow clone. Older history may be missing.")
        if snapshot.working_tree_changed:
            warnings.append("Working-tree edits are present. Selection uses the committed snapshot.")
        return Timeline(
            str(self.root), snapshot.path, self.commit, selection, entries, tuple(warnings)
        )


def parse_history(output: bytes) -> tuple[HistoryEntry, ...]:
    if not output.strip():
        return ()
    fields = output.split(b"\0")
    if fields[0].strip() or (len(fields) - 1) % 7:
        raise TrailError("Git returned an unexpected range-history format.")
    entries = []
    hunk_pattern = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.M)
    for offset in range(1, len(fields), 7):
        commit, parents, author, authored_at, committed_at, subject, raw_patch = fields[offset:offset + 7]
        patch = raw_patch.decode("utf-8", errors="replace").strip("\n")
        match = hunk_pattern.search(patch)
        if match is None:
            raise TrailError("Git returned a history entry without a selected-range diff.")
        old_start, old_count, new_start, new_count = match.groups()
        entries.append(
            HistoryEntry(
                commit.decode("ascii"),
                tuple(parents.decode("ascii").split()),
                author.decode("utf-8", errors="replace"),
                canonical_date(authored_at),
                canonical_date(committed_at),
                subject.decode("utf-8", errors="replace"),
                patch,
                int(old_start),
                int(old_count) if old_count is not None else 1,
                int(new_start),
                int(new_count) if new_count is not None else 1,
            )
        )
    return tuple(entries)


def canonical_date(value: bytes) -> str:
    try:
        return datetime.fromisoformat(value.decode("ascii").replace("Z", "+00:00")).isoformat()
    except (ValueError, UnicodeDecodeError) as error:
        raise TrailError("Git returned an invalid commit timestamp.") from error

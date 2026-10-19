from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from .git import HistoryEntry, HistoryView, LineRange, Repository, Snapshot, TrailError, patch_path

MAX_MERGES = 100
MAX_RENAME_CANDIDATES = 200
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.M)


def _git(repository, *args: str, timeout: int = 120) -> bytes:
    return repository._git("--no-replace-objects", *args, timeout=timeout)


class CanonicalRepository(Repository):
    def _git(self, *args: str, timeout: int = 120) -> bytes:
        if args[:1] != ("--no-replace-objects",):
            args = ("--no-replace-objects", *args)
        return super()._git(*args, timeout=timeout)

    def at(self, commit: str) -> CanonicalRepository:
        return canonical_at(self, commit)

    def revision_text(self, path: str, commit: str) -> str:
        path = self.path(path)
        self.validate_commit(commit)
        _require_object(self, commit, "commit")
        kind, blob = _file(self, commit, path)
        if kind != "blob":
            raise TrailError(f"Historical text file is unavailable: {path}.")
        return _blob_text(self, blob)


@lru_cache(maxsize=32)
def _canonical_at(repository, commit: str) -> CanonicalRepository:
    return CanonicalRepository(repository.root, commit)


def canonical_at(repository, commit: str) -> CanonicalRepository:
    repository.validate_commit(commit)
    _require_object(repository, commit, "commit")
    return _canonical_at(repository, commit)


@dataclass(frozen=True)
class CommitMetadata:
    commit: str
    tree: str
    parents: tuple[str, ...]
    author: str
    authored_at: str
    committed_at: str
    subject: str
    message: str


@dataclass(frozen=True)
class MergeList:
    starting_commit: str
    merges: tuple[CommitMetadata, ...]
    truncated: bool

    @property
    def entries(self) -> tuple[CommitMetadata, ...]:
        return self.merges


@dataclass(frozen=True)
class ParentComparison:
    metadata: CommitMetadata
    parent_metadata: CommitMetadata
    parent_index: int
    path: str
    old_path: str
    new_path: str
    old_blob: str
    new_blob: str
    patch: str
    old_ranges: tuple[LineRange, ...]
    new_ranges: tuple[LineRange, ...]
    before: HistoryView
    after: HistoryView
    note: str

    @property
    def commit(self) -> str:
        return self.metadata.commit

    @property
    def parent_commit(self) -> str:
        return self.parent_metadata.commit

    @property
    def parents(self) -> tuple[str, ...]:
        return self.metadata.parents

    @property
    def old_available(self) -> bool:
        return self.before.snapshot is not None

    @property
    def new_available(self) -> bool:
        return self.after.snapshot is not None

    @property
    def entry(self) -> HistoryEntry:
        old, new = self.before.selection, self.after.selection
        return HistoryEntry(
            self.commit, self.parents, self.metadata.author,
            self.metadata.authored_at, self.metadata.committed_at, self.metadata.subject,
            self.patch, old.start if old else 0, old.end - old.start + 1 if old else 0,
            new.start if new else 0, new.end - new.start + 1 if new else 0,
            self.old_path, self.new_path, old, new, "merge parent", None, self.note,
        )


def _require_object(repository, name: str, kind: str) -> None:
    actual = _git(repository, "cat-file", "-t", name).strip()
    if actual != kind.encode():
        raise TrailError(f"Expected a Git {kind} object at {name}.")


def _person(value: bytes) -> tuple[str, str]:
    match = re.fullmatch(rb"(.*) <[^>]*> (-?\d+) ([+-])(\d{2})(\d{2})", value)
    if match is None:
        raise TrailError("Git returned invalid commit author metadata.")
    name, stamp, sign, hours, minutes = match.groups()
    try:
        offset = int(hours) * 60 + int(minutes)
        if int(minutes) >= 60:
            raise ValueError("invalid timezone")
        zone = timezone(timedelta(minutes=offset if sign == b"+" else -offset))
        date = datetime.fromtimestamp(int(stamp), zone).isoformat()
    except (ValueError, OverflowError, OSError) as error:
        raise TrailError("Git returned an invalid commit timestamp.") from error
    return name.decode("utf-8", errors="replace"), date


@lru_cache(maxsize=128)
def _metadata(repository, commit: str) -> CommitMetadata:
    raw = _git(repository, "cat-file", "-p", commit)
    headers, separator, message = raw.partition(b"\n\n")
    if not separator:
        raise TrailError("Git returned an invalid commit object.")
    parents = []
    tree = author = committer = None
    for header in headers.split(b"\n"):
        if header.startswith(b"tree "):
            tree = header[5:].decode("ascii", errors="replace")
        elif header.startswith(b"parent "):
            parent = header[7:].decode("ascii", errors="replace")
            repository.validate_commit(parent)
            parents.append(parent)
        elif header.startswith(b"author "):
            author = _person(header[7:])
        elif header.startswith(b"committer "):
            committer = _person(header[10:])
    if tree is None or author is None or committer is None:
        raise TrailError("Git returned incomplete commit metadata.")
    repository.validate_commit(tree)
    text = message.decode("utf-8", errors="replace").strip()
    subject = " ".join(text.split("\n\n", 1)[0].splitlines())
    return CommitMetadata(commit, tree, tuple(parents), author[0], author[1],
                          committer[1], subject, text)


def commit_metadata(repository, commit: str) -> CommitMetadata:
    repository.validate_commit(commit)
    _require_object(repository, commit, "commit")
    return _metadata(repository, commit)


def _reachable_merges(repository, commit: str, limit: int) -> MergeList:
    output = _git(repository,
        "rev-list", "--merges", "--date-order", f"--max-count={limit + 1}", commit, "--",
    )
    commits = output.decode("ascii", errors="strict").splitlines()
    truncated = len(commits) > limit
    if _git(repository, "rev-parse", "--is-shallow-repository").strip() == b"true":
        roots = _git(repository,
            "rev-list", "--max-parents=0", "--date-order",
            f"--max-count={MAX_MERGES + 1}", commit, "--",
        ).decode("ascii", errors="strict").splitlines()
        truncated = truncated or len(roots) > MAX_MERGES
        boundaries = [
            item for item in roots[:MAX_MERGES]
            if len(commit_metadata(repository, item).parents) > 1 and item not in commits
        ]
        commits.extend(boundaries)
        commits.sort(key=lambda item: datetime.fromisoformat(commit_metadata(repository, item).committed_at),
                     reverse=True)
    return MergeList(commit, tuple(commit_metadata(repository, item) for item in commits[:limit]),
                     truncated or len(commits) > limit)


def reachable_merges(repository, commit: str, limit: int = MAX_MERGES) -> MergeList:
    if type(limit) is not int or not 1 <= limit <= MAX_MERGES:
        raise TrailError(f"List between 1 and {MAX_MERGES} reachable merges.")
    commit_metadata(repository, commit)
    result = _reachable_merges(repository, commit, limit)
    for item in result.merges:
        _require_object(repository, item.commit, "commit")
    return result


@dataclass(frozen=True)
class _Change:
    status: str
    old_path: str
    new_path: str


@lru_cache(maxsize=32)
def _changes(repository, parent: str, commit: str, renames: bool) -> tuple[_Change, ...]:
    raw = _git(repository,
        "diff-tree", "--no-commit-id", "--name-status", "-r", "-z",
        "--find-renames" if renames else "--no-renames",
        "--no-ext-diff", "--no-textconv", parent, commit, "--",
    )
    fields = raw.split(b"\0")
    if fields[-1] != b"":
        raise TrailError("Git returned invalid changed-file records.")
    fields.pop()
    records = []
    index = 0
    try:
        while index < len(fields):
            status = fields[index].decode("ascii")
            if not re.fullmatch(r"(?:[ADMTUXB]|[RC]\d+)", status):
                raise ValueError("invalid status")
            count = 2 if status[0] in "RC" else 1
            names = [repository.path(value.decode("utf-8")) for value in fields[index + 1:index + 1 + count]]
            if len(names) != count:
                raise ValueError("missing path")
            old = "" if status == "A" else names[0]
            new = "" if status == "D" else names[-1]
            records.append(_Change(status, old, new))
            index += count + 1
    except (UnicodeError, ValueError) as error:
        raise TrailError("Git returned invalid or non-UTF-8 changed-file paths.") from error
    return tuple(records)


def _file(repository, commit: str, path: str) -> tuple[str, str]:
    if not path:
        return "", ""
    raw = _git(repository, "ls-tree", "-z", commit, "--", path)
    if not raw:
        return "", ""
    fields = raw.split(b"\0")
    if len(fields) != 2 or fields[-1]:
        raise TrailError(f"Ambiguous committed file path: {path}.")
    header, separator, name = fields[0].partition(b"\t")
    parts = header.split()
    if not separator or len(parts) != 3 or name != path.encode():
        raise TrailError(f"Git returned an invalid file record for {path}.")
    kind, blob = parts[1].decode("ascii"), parts[2].decode("ascii")
    repository.validate_commit(blob)
    _require_object(repository, blob, kind)
    return kind, blob


def _check_rename(repository, parent: str, commit: str, path: str, *, renamed: bool = False) -> None:
    raw = _changes(repository, parent, commit, False)
    if not any(item.status == "A" and item.new_path == path for item in raw):
        return
    deleted = [item.old_path for item in raw if item.status == "D"]
    if len(deleted) > MAX_RENAME_CANDIDATES:
        raise TrailError(f"Rename candidate limit reached for {path}; its predecessor is unavailable.")
    kind, blob = _file(repository, commit, path)
    matches = [candidate for candidate in deleted if _file(repository, parent, candidate) == (kind, blob)]
    if len(matches) > 1:
        raise TrailError(f"Ambiguous rename for {path}: multiple deleted files have identical contents.")
    if renamed:
        added = [item.new_path for item in raw if item.status == "A"]
        if len(added) > MAX_RENAME_CANDIDATES:
            raise TrailError(f"Rename candidate limit reached for {path}; its destination is unavailable.")
        matches = [candidate for candidate in added if _file(repository, commit, candidate) == (kind, blob)]
        if len(matches) > 1:
            raise TrailError(f"Ambiguous rename for {path}: multiple added files have identical contents.")


def _patch(repository, parent: str, commit: str, old_path: str, new_path: str) -> str:
    paths = tuple(dict.fromkeys(path for path in (old_path, new_path) if path))
    if not paths:
        return ""
    raw = _git(repository,
        "-c", "diff.suppressBlankEmpty=false",
        "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-relative",
        "--find-renames", "--src-prefix=a/", "--dst-prefix=b/", "--unified=3",
        "--output-indicator-new=+", "--output-indicator-old=-", "--output-indicator-context= ",
        parent, commit, "--", *paths,
    )
    patch = raw.decode("utf-8", errors="replace").strip("\n")
    sections = re.split(r"(?=^diff --git )", patch, flags=re.M)
    sections = [section.rstrip("\n") for section in sections if section]
    if len(sections) > 1:
        raise TrailError("Ambiguous file comparison: the old and new paths include multiple changed files.")
    if sections and any(line.startswith("--- ") for line in patch.split("\n")):
        if (patch_path(patch, "--- ", "a/"), patch_path(patch, "+++ ", "b/")) != (old_path, new_path):
            raise TrailError("Git returned a patch for a different file pair.")
    return patch


def _ranges(patch: str, side: int) -> tuple[LineRange, ...]:
    return tuple(
        LineRange(int(hunk[side]), int(hunk[side]) + int(hunk[side + 1] or "1") - 1,
                  "full-file comparison")
        for hunk in HUNK.findall(patch) if int(hunk[side + 1] or "1") > 0
    )


@lru_cache(maxsize=64)
def _blob_text(repository, blob: str) -> str:
    raw = _git(repository, "cat-file", "blob", blob)
    if b"\0" in raw:
        raise TrailError("This is a binary file. Select a text file.")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise TrailError("This file is not UTF-8 text. Select a UTF-8 source file.") from error


def _source(repository, commit: str, path: str, kind: str, blob: str, ranges: tuple[LineRange, ...],
            note: str) -> HistoryView:
    if not path:
        return HistoryView(None, None, note + " · File does not exist on this side.")
    if kind != "blob":
        return HistoryView(None, None, note + " · This side is not a text-file blob.")
    try:
        text = _blob_text(repository, blob)
    except TrailError as error:
        return HistoryView(None, None, note + " · " + str(error))
    snapshot = Snapshot(path, commit, text, False)
    for selection in ranges:
        selection.validate(len(snapshot.lines))
    selected = LineRange(min(item.start for item in ranges), max(item.end for item in ranges),
                         "full-file comparison") if ranges else None
    if not snapshot.lines:
        note += " · Empty committed file."
    return HistoryView(snapshot, selected, note)


@lru_cache(maxsize=32)
def _parent_comparison(repository, commit: str, path: str, parent: str) -> ParentComparison:
    metadata = commit_metadata(repository, commit)
    parent_metadata = commit_metadata(repository, parent)
    records = _changes(repository, parent, commit, True)
    candidates = [item for item in records if item.new_path == path]
    if not candidates:
        candidates = [item for item in records if item.old_path == path]
    if len(candidates) > 1:
        raise TrailError(f"Ambiguous file mapping for {path}.")
    if candidates:
        change = candidates[0]
        old_path, new_path = change.old_path, change.new_path
        if change.status[0] in "AR":
            _check_rename(repository, parent, commit, new_path, renamed=change.status[0] == "R")
    else:
        old_path = path if _file(repository, parent, path)[0] else ""
        new_path = path if _file(repository, commit, path)[0] else ""
    old_kind, old_blob = _file(repository, parent, old_path)
    new_kind, new_blob = _file(repository, commit, new_path)
    patch = _patch(repository, parent, commit, old_path, new_path)
    old_ranges, new_ranges = _ranges(patch, 0), _ranges(patch, 2)
    index = metadata.parents.index(parent) + 1
    note = f"FULL-FILE merge comparison · parent {index}/{len(metadata.parents)} · {parent} → {commit}"
    if old_path and new_path and old_path != new_path:
        note += " · Git-detected rename; no semantic lineage inferred."
    if not patch:
        note += " · No file changes against this parent."
    before = _source(repository, parent, old_path, old_kind, old_blob, old_ranges, note)
    after = _source(repository, commit, new_path, new_kind, new_blob, new_ranges, note)
    return ParentComparison(metadata, parent_metadata, index, path, old_path, new_path,
                            old_blob, new_blob, patch, old_ranges, new_ranges, before, after, note)


def _check_comparison_objects(repository, comparison: ParentComparison) -> None:
    for metadata in (comparison.metadata, comparison.parent_metadata):
        _require_object(repository, metadata.commit, "commit")
        _require_object(repository, metadata.tree, "tree")
    for commit, path, blob in (
        (comparison.parent_commit, comparison.old_path, comparison.old_blob),
        (comparison.commit, comparison.new_path, comparison.new_blob),
    ):
        if path and blob:
            _require_object(repository, f"{commit}:{path}", _git(repository, "cat-file", "-t", blob).strip().decode())


def parent_comparison(repository, commit: str, path: str, parent: int | str = 1) -> ParentComparison:
    metadata = commit_metadata(repository, commit)
    if len(metadata.parents) < 2:
        raise TrailError("Choose a merge commit with at least two parents.")
    if type(parent) is int:
        if not 1 <= parent <= len(metadata.parents):
            raise TrailError(f"Choose a parent from 1 to {len(metadata.parents)}.")
        chosen = metadata.parents[parent - 1]
    elif isinstance(parent, str):
        chosen = parent.lower()
        repository.validate_commit(chosen)
        if chosen not in metadata.parents:
            raise TrailError("The chosen SHA is not a parent of this merge.")
    else:
        raise TrailError("Choose a 1-based parent number or its exact SHA.")
    try:
        commit_metadata(repository, chosen)
    except TrailError as error:
        raise TrailError(f"Parent {metadata.parents.index(chosen) + 1} ({chosen}) is unavailable: {error}") from error
    result = _parent_comparison(repository, commit, repository.path(path), chosen)
    _check_comparison_objects(repository, result)
    return result


def parent_source(repository, comparison: ParentComparison, before: bool = False) -> HistoryView:
    if not isinstance(comparison, ParentComparison):
        raise TrailError("Choose an exact merge-parent comparison.")
    expected = parent_comparison(repository, comparison.commit, comparison.path, comparison.parent_commit)
    if expected != comparison:
        raise TrailError("The merge-parent comparison no longer matches its committed file.")
    return comparison.before if before else comparison.after


def parent_diff_source(repository, comparison: ParentComparison, line: int, before: bool = False) -> HistoryView:
    view = parent_source(repository, comparison, before)
    if view.snapshot is None:
        raise TrailError(view.note)
    if type(line) is not int:
        raise TrailError("Choose an integer source line from this parent comparison.")
    ranges = comparison.old_ranges if before else comparison.new_ranges
    if not any(selection.start <= line <= selection.end for selection in ranges):
        raise TrailError("This parent comparison has no diff source line at that position.")
    selection = LineRange(line, line, "diff line")
    selection.validate(len(view.snapshot.lines))
    note = f"{'Before' if before else 'After'} · {view.snapshot.path}:{line} · {view.snapshot.commit}"
    return HistoryView(view.snapshot, selection, comparison.note + " · " + note)

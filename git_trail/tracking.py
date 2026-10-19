from __future__ import annotations

import difflib
import json
import re
import time
from dataclasses import dataclass
from pathlib import PurePosixPath

from .git import HistoryEntry, LineRange, Snapshot, Timeline, TrailError, canonical_date
from .selectors import LANGUAGES, Symbol, symbols

MAX_COMMITS = 300
MAX_FILES = 200
MAX_BYTES = 2_000_000
MAX_TOKENS = 4000
MAX_TOTAL_BYTES = 20_000_000
MAX_SECONDS = 45
MIN_SIMILARITY = 0.90


@dataclass(frozen=True)
class Definition:
    path: str
    symbol: Symbol
    scope: tuple[str, ...]
    body: str
    tokens: tuple[str, ...]


def definitions(path: str, text: str) -> tuple[Definition, ...]:
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise TrailError(f"Symbol scan byte limit reached in {path}.")
    items = symbols(text, path)
    lines = text.split("\n")
    result = []
    for item in items:
        body = "\n".join(lines[item.start - 1:item.end])
        tokens = tuple(
            "__symbol__" if token == item.name else token
            for token in re.findall(r""" "(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\w+|[^\w\s] """, body, re.X)
        )
        if len(tokens) > MAX_TOKENS:
            raise TrailError(f"Symbol scan token limit reached in {path}:{item.start}.")
        scope = tuple(
            outer.name for outer in items
            if outer != item and outer.start <= item.start and outer.end >= item.end
        )
        result.append(Definition(path, item, scope, body, tokens))
    return tuple(result)


def symbol_patch(old: Definition | None, new: Definition) -> str:
    old_lines = old.body.split("\n") if old else []
    new_lines = new.body.split("\n")
    old_path = json.dumps("a/" + old.path, ensure_ascii=False) if old else "/dev/null"
    new_path = json.dumps("b/" + new.path, ensure_ascii=False)
    patch = list(difflib.unified_diff(old_lines, new_lines, old_path, new_path, lineterm=""))
    if not patch:
        return "\n".join([
            f"--- {old_path}", f"+++ {new_path}",
            f"@@ -{old.symbol.start},{len(old_lines)} +{new.symbol.start},{len(new_lines)} @@",
            *(" " + line for line in new_lines),
        ])
    hunk = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
    for index, line in enumerate(patch):
        match = hunk.match(line)
        if match:
            a, ac, b, bc, suffix = match.groups()
            a = int(a) + (old.symbol.start - 1 if old else 0)
            b = int(b) + new.symbol.start - 1
            patch[index] = f"@@ -{a},{ac or '1'} +{b},{bc or '1'} @@{suffix}"
    return "\n".join(patch)


def follow_symbol(repository, snapshot: Snapshot, selection: LineRange, *, cancel=None) -> Timeline:
    if snapshot.commit != repository.commit or snapshot.path != repository.path(snapshot.path):
        raise TrailError("This selection belongs to a different repository snapshot.")
    selection.validate(len(snapshot.lines))
    starting = definitions(snapshot.path, snapshot.text)
    matches = [item for item in starting if item.symbol.start == selection.start and item.symbol.end == selection.end]
    if len(matches) != 1:
        raise TrailError("Follow a complete function or class. Choose a symbol or use :follow.")
    current = matches[0]
    language = LANGUAGES[PurePosixPath(current.path).suffix.lower()]
    deadline = time.monotonic() + MAX_SECONDS
    warnings = [
        "Refactor-aware symbol history follows first parents. Similarity scores are heuristics, not semantic proof.",
        "Same-path names with matching scope continue through body edits; this is named continuity, not proof of semantic identity.",
    ]
    shallow = False
    if snapshot.working_tree_changed:
        warnings.append("Working-tree edits are present. Selection uses the committed snapshot.")
    rows = []
    entries = []
    cache = {}
    metadata = {}
    total_bytes = 0

    def check() -> None:
        if cancel is not None and cancel.is_set():
            raise TrailError("Symbol history cancelled.")
        if time.monotonic() >= deadline:
            raise TrailError("Symbol history time limit reached.")

    def read(commit: str, path: str):
        nonlocal total_bytes
        check()
        key = (commit, path)
        if key not in cache:
            size_record = info(commit, path)
            if not size_record:
                raise TrailError(f"Historical file is unavailable: {path}.")
            size = size_record.split(b"\t", 1)[0].split()[-1]
            if not size.isdigit() or int(size) > MAX_BYTES:
                raise TrailError(f"Symbol scan byte limit reached in {path}.")
            total_bytes += int(size)
            if total_bytes > MAX_TOTAL_BYTES:
                raise TrailError("Symbol scan total byte limit reached.")
            raw = git("show", f"{commit}:{repository.path(path)}")
            if b"\0" in raw:
                raise TrailError(f"{path} is a binary file.")
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError as error:
                raise TrailError(f"{path} is not UTF-8 text.") from error
            cache[key] = definitions(path, text)
        return cache[key]

    def git(*args):
        check()
        return repository._git(*args, timeout=max(0.1, min(10, deadline - time.monotonic())))

    def info(commit: str, path: str):
        key = (commit, path)
        if key not in metadata:
            metadata[key] = git("ls-tree", "-l", "-z", commit, "--", repository.path(path))
        return metadata[key]

    def add(commit: str, parents: tuple[str, ...], previous, method: str, score, note: str = "") -> None:
        check()
        raw = git("show", "--no-patch", "--no-show-signature", "--format=%an%x00%aI%x00%cI%x00%s", commit).strip(b"\n").split(b"\0")
        author, authored, committed, subject = raw
        old = previous.symbol.selection if previous else None
        new = current.symbol.selection
        entries.append(HistoryEntry(
            commit, parents, author.decode("utf-8", errors="replace"),
            canonical_date(authored), canonical_date(committed), subject.decode("utf-8", errors="replace"),
            symbol_patch(previous, current), old.start if old else 0, old.end - old.start + 1 if old else 0,
            new.start, new.end - new.start + 1, previous.path if previous else "", current.path,
            old, new, method, score, note,
        ))

    try:
        shallow = git("rev-parse", "--is-shallow-repository").strip() == b"true"
        if shallow:
            warnings.append("Shallow clone: history stops at the visible boundary.")
        rows = git("rev-list", "--first-parent", f"--max-count={MAX_COMMITS + 1}", "--parents", snapshot.commit).decode().splitlines()
        for row in rows[:MAX_COMMITS]:
            check()
            commit, *parents = row.split()
            if not parents:
                note = "Shallow boundary: earlier source is unavailable." if shallow else ""
                add(commit, (), None, "boundary" if shallow else "introduction", None, note)
                break
            parent = parents[0]
            changed = git(
                "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "--no-renames",
                "--no-ext-diff", "--no-textconv", parent, commit, "--",
            ).split(b"\0")
            paths = tuple(dict.fromkeys(path.decode("utf-8", errors="strict") for path in changed if path))
            if current.path not in paths:
                continue
            same_file = read(parent, current.path) if info(parent, current.path) else ()
            named = [
                item for item in same_file
                if item.symbol.name == current.symbol.name and item.symbol.kind == current.symbol.kind and item.scope == current.scope
            ]
            if len(named) > 1:
                raise TrailError(f"Ambiguous symbol lineage at {commit[:10]}: repeated {current.symbol.name!r} definitions.")
            if named:
                previous, method, score = named[0], "name", None
            else:
                eligible = [
                    path for path in paths if path != current.path
                    and LANGUAGES.get(PurePosixPath(path).suffix.lower()) == language
                ]
                if len(eligible) > MAX_FILES:
                    raise TrailError(f"Symbol candidate file limit reached at {commit[:10]}.")
                candidates = list(same_file)
                for path in eligible:
                    if info(parent, path):
                        candidates.extend(read(parent, path))
                ranked = []
                copies = []
                for candidate in candidates:
                    check()
                    if candidate.symbol.kind != current.symbol.kind:
                        continue
                    exact = candidate.tokens == current.tokens
                    score = 1.0 if exact else difflib.SequenceMatcher(None, candidate.tokens, current.tokens, autojunk=False).ratio()
                    if not exact and (score < MIN_SIMILARITY or len(current.tokens) < 20):
                        continue
                    remains = read(commit, candidate.path) if info(commit, candidate.path) else ()
                    retained = any(
                        item.symbol.name == candidate.symbol.name and item.symbol.kind == candidate.symbol.kind
                        and item.scope == candidate.scope for item in remains
                    )
                    if retained:
                        copies.append(candidate)
                    else:
                        ranked.append((score, candidate))
                ranked.sort(key=lambda item: item[0], reverse=True)
                if copies:
                    raise TrailError(f"Copy-like or ambiguous lineage at {commit[:10]}: an earlier definition is still present. No origin was inferred.")
                if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.05:
                    choices = ", ".join(f"{item.path}:{item.symbol.start} ({item.symbol.name})" for _, item in ranked[:4])
                    raise TrailError(f"Ambiguous symbol lineage at {commit[:10]}: {choices}.")
                if not ranked:
                    if candidates:
                        raise TrailError(f"No confident predecessor at {commit[:10]}. History is partial; unrelated definitions were not linked.")
                    add(commit, tuple(parents), None, "introduction", None)
                    break
                score, previous = ranked[0]
                method = "body" if score == 1 else "similarity"
            if (previous.path, previous.symbol.name, previous.scope, previous.body) != (current.path, current.symbol.name, current.scope, current.body):
                note = f"Followed {previous.path}:{previous.symbol.name} using {method}"
                if score is not None:
                    note += f" (similarity {score:.2f})"
                if len(parents) > 1:
                    note += ". Before view uses the first parent."
                add(commit, tuple(parents), previous, method, score, note)
            current = previous
        else:
            if len(rows) > MAX_COMMITS:
                warnings.append(f"Partial history: stopped after {MAX_COMMITS} first-parent commits.")
    except (TrailError, UnicodeDecodeError) as error:
        if cancel is not None and cancel.is_set():
            raise TrailError("Symbol history cancelled.") from error
        warnings.append(f"Partial symbol history: {error}")
    return Timeline(str(repository.root), snapshot.path, snapshot.commit, selection, tuple(entries), tuple(warnings), "symbol")

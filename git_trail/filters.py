from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from datetime import date
from pathlib import PurePosixPath

from .git import HistoryEntry, TrailError


def tokens(query: str) -> list[str]:
    try:
        return shlex.split(query)
    except ValueError as error:
        raise TrailError('Close the quote in your filter. Example: author:"Jane Doe"') from error


def filter_files(paths: tuple[str, ...], query: str) -> tuple[str, ...]:
    parts = tokens(query)
    extensions = [part[4:].casefold().lstrip(".") for part in parts if part.startswith("ext:")]
    words = [part.casefold() for part in parts if not part.startswith("ext:")]
    scored = []
    for path in paths:
        if extensions and PurePosixPath(path).suffix.casefold().lstrip(".") not in extensions:
            continue
        folded = path.casefold()
        score = 0
        for word in words:
            if word in folded:
                score += folded.index(word)
                continue
            cursor = 0
            positions = []
            for character in word:
                position = folded.find(character, cursor)
                if position < 0:
                    break
                positions.append(position)
                cursor = position + 1
            else:
                score += 1000 + (positions[-1] - positions[0] if positions else 0)
                continue
            break
        else:
            scored.append((score, path))
    return tuple(path for _, path in sorted(scored))


@dataclass(frozen=True)
class HistoryFilter:
    words: tuple[str, ...] = ()
    authors: tuple[str, ...] = ()
    hashes: tuple[str, ...] = ()
    after: str = ""
    before: str = ""

    @classmethod
    def parse(cls, query: str) -> HistoryFilter:
        values: dict[str, list[str]] = {"words": [], "authors": [], "hashes": []}
        dates = {"after": "", "before": ""}
        for token in tokens(query):
            key, separator, value = token.partition(":")
            if separator and key in {"author", "hash", "after", "before"}:
                if not value:
                    raise TrailError(f"Give {key}: a value.")
                if key in dates:
                    try:
                        if date.fromisoformat(value).isoformat() != value:
                            raise ValueError
                    except ValueError as error:
                        raise TrailError(f"Use {key}:YYYY-MM-DD, such as {key}:2026-01-01.") from error
                    dates[key] = value
                else:
                    values["authors" if key == "author" else "hashes"].append(value.casefold())
            else:
                values["words"].append(token.casefold())
        if dates["after"] and dates["before"] and dates["after"] > dates["before"]:
            raise TrailError("The after date must be on or before the before date.")
        return cls(**{key: tuple(value) for key, value in values.items()}, **dates)

    def matches(self, entry: HistoryEntry) -> bool:
        day = entry.committed_at[:10]
        haystack = "\n".join((entry.subject, entry.author, entry.commit, entry.patch)).casefold()
        return (
            all(word in haystack for word in self.words)
            and all(author in entry.author.casefold() for author in self.authors)
            and all(entry.commit.startswith(value) for value in self.hashes)
            and (not self.after or day >= self.after)
            and (not self.before or day <= self.before)
        )


def find_matches(
    text: str, query: str, *, regex: bool = False, case_sensitive: bool = False,
) -> tuple[tuple[tuple[int, int], tuple[int, int]], ...]:
    if not query:
        return ()
    try:
        pattern = re.compile(query if regex else re.escape(query), 0 if case_sensitive else re.IGNORECASE)
    except re.error as error:
        raise TrailError(f"Invalid expression: {error.msg}") from error
    from bisect import bisect_right

    starts = [0] + [index + 1 for index, character in enumerate(text) if character == "\n"]
    result = []
    for match in pattern.finditer(text):
        if match.start() == match.end():
            continue
        start_row = bisect_right(starts, match.start()) - 1
        end_row = bisect_right(starts, match.end()) - 1
        result.append((
            (start_row, match.start() - starts[start_row]),
            (end_row, match.end() - starts[end_row]),
        ))
    return tuple(result)

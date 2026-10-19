from __future__ import annotations

import re
from dataclasses import dataclass, replace

from rich.cells import cell_len
from rich.text import Text

from .git import display_source

HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class DiffLine:
    text: str
    kind: str = "metadata"
    old: int | None = None
    new: int | None = None
    hunk: int | None = None


@dataclass(frozen=True)
class DiffHunk:
    header: int
    changed: int
    end: int
    valid: bool


@dataclass(frozen=True)
class SplitRow:
    old: int | None = None
    new: int | None = None
    heading: int | None = None

    @property
    def indices(self) -> tuple[int, ...]:
        return tuple(index for index in (self.old, self.new, self.heading) if index is not None)


@dataclass(frozen=True)
class DiffPatch:
    lines: tuple[DiffLine, ...]
    hunks: tuple[DiffHunk, ...]

    @classmethod
    def parse(cls, patch: str) -> DiffPatch:
        lines = [DiffLine(text) for text in patch.split("\n")]
        hunks = []
        index = 0
        while index < len(lines):
            match = HEADER.match(lines[index].text)
            if match is None:
                index += 1
                continue
            header = index
            old, old_count, new, new_count = (int(value) if value else 1 for value in match.groups())
            remaining_old, remaining_new = old_count, new_count
            hunk = len(hunks)
            lines[index] = replace(lines[index], kind="hunk", hunk=hunk)
            changed = header
            overfull = False
            index += 1
            while index < len(lines):
                text = lines[index].text
                if text.startswith("\\ No newline at end of file"):
                    lines[index] = replace(lines[index], kind="marker", hunk=hunk)
                    index += 1
                    continue
                if remaining_old == remaining_new == 0:
                    overfull = text[:1] in (" ", "-", "+")
                    break
                prefix = text[:1]
                used_old, used_new = prefix in (" ", "-"), prefix in (" ", "+")
                if not (used_old or used_new) or used_old > remaining_old or used_new > remaining_new:
                    break
                kind = {" ": "context", "-": "delete", "+": "add"}[prefix]
                lines[index] = DiffLine(text, kind, old if used_old else None, new if used_new else None, hunk)
                if kind in ("delete", "add") and changed == header:
                    changed = index
                old += used_old
                new += used_new
                remaining_old -= used_old
                remaining_new -= used_new
                index += 1
            valid = not overfull and remaining_old == remaining_new == 0 and (old_count == 0 or int(match[1]) > 0) and (new_count == 0 or int(match[3]) > 0)
            if not valid:
                for row in range(header + 1, index):
                    lines[row] = replace(lines[row], old=None, new=None)
            hunks.append(DiffHunk(header, changed, index, valid))
        return cls(tuple(lines), tuple(hunks))

    def change_targets(self) -> tuple[int, ...]:
        targets = []
        for hunk in self.hunks:
            if not hunk.valid:
                continue
            edits = []
            inside = False
            for index in range(hunk.header + 1, hunk.end):
                kind = self.lines[index].kind
                if kind in ("delete", "add"):
                    if not inside:
                        edits.append(index)
                    inside = True
                elif kind != "marker":
                    inside = False
            if not edits:
                edits = [index for index in range(hunk.header + 1, hunk.end)
                         if self.lines[index].kind == "context"][:1]
            targets.extend(edits)
        return tuple(targets)

    def split_rows(self) -> tuple[SplitRow, ...]:
        rows = []
        removed, added, markers = [], [], []

        def flush() -> None:
            for offset in range(max(len(removed), len(added))):
                rows.append(SplitRow(removed[offset] if offset < len(removed) else None,
                                     added[offset] if offset < len(added) else None))
            rows.extend(SplitRow(heading=index) for index in markers)
            removed.clear()
            added.clear()
            markers.clear()

        for index, line in enumerate(self.lines):
            if line.kind == "delete":
                removed.append(index)
            elif line.kind == "add":
                added.append(index)
            elif line.kind == "marker" and (removed or added):
                markers.append(index)
            else:
                flush()
                rows.append(SplitRow(index, index) if line.kind == "context" else SplitRow(heading=index))
        flush()
        return tuple(rows)

    def side_by_side(self, old_path: str, new_path: str, width: int, active: int | None = None, *, compact: bool = False) -> tuple[Text, tuple[SplitRow, ...], int]:
        rows = self.split_rows()
        if compact and self.hunks:
            rows = tuple(row for row in rows if any(index >= self.hunks[0].header for index in row.indices))
        digits = max(3, max((len(str(number)) for line in self.lines for number in (line.old, line.new) if number), default=1))
        old_label = "BEFORE · " + display_source(old_path or "(absent)").replace("\n", "␤").replace("\t", "␉")
        new_label = "AFTER · " + display_source(new_path or "(absent)").replace("\n", "␤").replace("\t", "␉")

        def cell(index: int | None, before: bool) -> str:
            if index is None:
                return " " * (digits + 2)
            line = self.lines[index]
            number = line.old if before else line.new
            gutter = str(number).rjust(digits) if number is not None else " " * digits
            return gutter + " " + line.text[:1] + display_source(line.text[1:]).expandtabs(4)

        left_width = max((width - 3) // 2, cell_len(old_label),
                         max((cell_len(cell(row.old, True)) for row in rows if row.heading is None), default=0))
        result = Text(no_wrap=True, overflow="ignore")
        result.append(old_label + " " * max(0, left_width - cell_len(old_label)) + " │ " + new_label + "\n", style="#7aa2f7")
        for row in rows:
            highlighted = active is not None and active in row.indices
            if row.heading is not None:
                result.append(display_source(self.lines[row.heading].text), style="#7aa2f7" if self.lines[row.heading].kind == "hunk" else "#565f89")
            else:
                left = cell(row.old, True)
                result.append(left, style=self.color(row.old))
                result.append(" " * max(0, left_width - cell_len(left)) + " │ ", style="#3b4261")
                result.append(cell(row.new, False), style=self.color(row.new))
            result.append("\n")
            if highlighted:
                start = result.plain.rfind("\n", 0, len(result) - 1) + 1
                result.stylize("on #283457", start, len(result) - 1)
        return result, rows, left_width + 1

    def color(self, index: int | None) -> str:
        if index is None:
            return "#565f89"
        return {"add": "#9ece6a", "delete": "#f7768e"}.get(self.lines[index].kind, "#c0caf5")

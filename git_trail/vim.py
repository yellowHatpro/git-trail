from __future__ import annotations

import re
from bisect import bisect_left, bisect_right

from textual.widgets.text_area import Selection


class VimCursor:
    """Read-only normal and visual motions over source characters."""

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.mode = "NORMAL"
        self.anchor = None
        self.cursor = (0, 0)
        self.expected = None
        self.column = None
        self.prefix = ""
        self.count = ""

    def sync(self, view) -> None:
        if self.expected != view.selection:
            self.reset()
            self.cursor = view.cursor_location
            self.expected = view.selection
        self.cursor = self.clamp(view, self.cursor)

    @staticmethod
    def lines(view) -> list[str]:
        return view.text.splitlines() or [""]

    def clamp(self, view, location):
        lines = self.lines(view)
        row = max(0, min(location[0], len(lines) - 1))
        return row, max(0, min(location[1], max(0, len(lines[row]) - 1)))

    def paint(self, view) -> None:
        lines = self.lines(view)
        if self.mode == "NORMAL":
            selection = Selection.cursor(self.cursor)
        elif self.mode == "V-LINE":
            first, last = sorted((self.anchor[0], self.cursor[0]))
            end = (last + 1, 0) if last + 1 < view.document.line_count else (last, len(lines[last]))
            selection = Selection((first, 0), end)
        else:
            first, last = sorted((self.anchor, self.cursor))
            end = (last[0], min(last[1] + 1, len(lines[last[0]])))
            selection = Selection(first, end) if self.cursor >= self.anchor else Selection(end, first)
        view.selection = selection
        self.expected = selection
        view.scroll_to(
            x=max(0, self.cursor[1] - max(1, view.size.width - 8)),
            y=max(0, self.cursor[0] - max(1, view.size.height // 2)),
            animate=False,
        )
        view.app.set_editor_mode(self.mode)

    def leave_visual(self, view, *, collapse: bool = True) -> None:
        self.sync(view)
        self.mode = "NORMAL"
        self.anchor = None
        self.prefix = self.count = ""
        if collapse:
            self.paint(view)
        else:
            view.app.set_editor_mode("NORMAL")

    def word(self, view, forward: bool, count: int):
        lines = self.lines(view)
        starts = [0]
        for line in lines[:-1]:
            starts.append(starts[-1] + len(line) + 1)
        text = "\n".join(lines)
        words = [match.start() for match in re.finditer(r"\w+|[^\w\s]+", text)]
        offset = starts[self.cursor[0]] + self.cursor[1]
        index = bisect_right(words, offset) + count - 1 if forward else bisect_left(words, offset) - count
        if index < 0:
            offset = 0
        elif index >= len(words):
            offset = max(0, len(text) - 1)
        else:
            offset = words[index]
        row = bisect_right(starts, offset) - 1
        return self.clamp(view, (row, offset - starts[row]))

    def handle(self, view, key: str) -> bool:
        self.sync(view)
        if key == "escape":
            self.leave_visual(view)
            return True
        if key in ("v", "V"):
            mode = "VISUAL" if key == "v" else "V-LINE"
            if self.mode == mode:
                self.leave_visual(view)
            else:
                self.mode = mode
                self.anchor = self.anchor or self.cursor
                self.paint(view)
            return True
        if key in "0123456789" and len(key) == 1 and (key != "0" or self.count):
            self.count = (self.count + key)[:4]
            return True
        count = max(1, int(self.count or "1"))
        if key == "g" and self.prefix != "g":
            self.prefix = "g"
            return True
        row, col = self.cursor
        lines = self.lines(view)
        if key == "g" and self.prefix == "g":
            location = (count - 1, 0)
        elif key == "G":
            location = (count - 1 if self.count else len(lines) - 1, 0)
        elif key in ("j", "k"):
            if self.column is None:
                self.column = col
            location = (row + count * (1 if key == "j" else -1), self.column)
        elif key in ("h", "l"):
            location = (row, col + count * (1 if key == "l" else -1))
        elif key == "0":
            location = (row, 0)
        elif key == "$":
            location = (row, len(lines[row]) - 1)
        elif key in ("w", "b"):
            location = self.word(view, key == "w", count)
        else:
            self.prefix = self.count = ""
            return False
        if key not in ("j", "k"):
            self.column = None
        self.prefix = self.count = ""
        self.cursor = self.clamp(view, location)
        self.paint(view)
        return True

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.content import Content
from textual.visual import Visual
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from .git import display_source


def display_path(path: str) -> str:
    return display_source(path).replace("\n", "␤").replace("\t", "⇥")


def _prefix(text: str, width: int) -> str:
    value = Text(text)
    value.truncate(max(0, width), overflow="crop")
    return value.plain


def _suffix(text: str, width: int) -> str:
    result = ""
    for character in reversed(text):
        if cell_len(character + result) > width:
            break
        result = character + result
    return result


def _filename(name: str, width: int) -> str:
    if width <= 0:
        return ""
    if cell_len(name) <= width:
        return name
    extension = PurePosixPath(name).suffix
    tail = extension if extension and cell_len(extension) < width else _suffix(name, max(0, width // 2))
    return _prefix(name, width - cell_len(tail) - 1) + "…" + tail


@dataclass(frozen=True)
class ExplorerRow:
    path: str
    name: str
    directory: bool = False
    depth: int = 0
    parent: str = ""

    @property
    def id(self) -> str:
        return self.path + "/" if self.directory else self.path


class ExplorerLabel(Visual):
    def __init__(self, row: ExplorerRow, *, expanded: bool = False, search: bool = False):
        self.row = row
        self.expanded = expanded
        self.search = search

    @property
    def plain(self) -> str:
        row = self.row
        if self.search:
            context = row.path.rpartition("/")[0]
            return display_path(row.name) + (f"  ·  {display_path(context)}" if context else "")
        marker = "▾ " if self.expanded else "▸ " if row.directory else "  "
        return "  " * row.depth + marker + display_path(row.name)

    def __str__(self) -> str:
        return self.plain

    def get_height(self, rules, width: int) -> int:
        return 1

    def get_optimal_width(self, rules, container_width: int) -> int:
        return min(cell_len(self.plain), container_width)

    def render_strips(self, width: int, height, style, options):
        row = self.row
        name = display_path(row.name)
        if self.search:
            context = row.path.rpartition("/")[0]
            context = display_path(context)
            context_width = min(cell_len(context), max(3, width // 4)) if context and width >= 10 else 0
            name_width = max(4, width - context_width - 3) if context_width else width
            text = Text(_filename(name, name_width), no_wrap=True)
            remaining = width - text.cell_len - 3
            if context and remaining > 0:
                context = context if cell_len(context) <= remaining else "…" + _suffix(context, remaining - 1)
                text.append(" · " + context, style="dim")
        else:
            indent = min(row.depth * 2, max(0, width - min(10, cell_len(name)) - 2))
            marker = "▾ " if self.expanded else "▸ " if row.directory else "  "
            text = Text(" " * indent + marker, no_wrap=True)
            text.append(_filename(name, width - indent - 2), style="bold" if row.directory else "")
        return Content.from_rich_text(text).render_strips(width, height, style, options)


@dataclass
class _Directory:
    path: str
    name: str
    parent: str = ""
    directories: dict[str, _Directory] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)


class FileExplorer(OptionList):
    """Committed paths with cached directories and explicit visible rows."""

    def __init__(self, *args, **kwargs):
        self.paths: tuple[str, ...] | None = None
        self.rows: tuple[ExplorerRow, ...] = ()
        self.expanded: set[str] = set()
        self.search_mode = False
        self._directories: dict[str, _Directory] = {}
        self._aliases: dict[str, str] = {}
        self._file_parents: dict[str, str] = {}
        self._row_indices: dict[str, int] = {}
        self._tree_selected: str | None = None
        super().__init__(*args, **kwargs)

    @property
    def selected_row(self) -> ExplorerRow | None:
        return self.row_at(self.highlighted)

    def row_at(self, index: int | None) -> ExplorerRow | None:
        return self.rows[index] if index is not None and 0 <= index < len(self.rows) else None

    def set_paths(self, paths: tuple[str, ...]) -> None:
        if paths is self.paths or paths == self.paths:
            return
        self.paths = paths
        root = _Directory("", "")
        raw = {"": root}
        file_parents = {}
        for path in paths:
            parts = path.split("/")
            parent = root
            prefix = ""
            for name in parts[:-1]:
                prefix = f"{prefix}/{name}" if prefix else name
                directory = raw.get(prefix)
                if directory is None:
                    directory = raw[prefix] = _Directory(prefix, name)
                    parent.directories[name] = directory
                parent = directory
            parent.files.append(path)
            file_parents[path] = parent.path
        aliases = {"": ""}
        directories = {"": root}
        stack = [root]
        while stack:
            parent = stack.pop()
            children = {}
            for child in sorted(parent.directories.values(), key=lambda item: (item.name.casefold(), item.name)):
                chain = [child]
                while not child.files and len(child.directories) == 1:
                    child = next(iter(child.directories.values()))
                    chain.append(child)
                child.name = "/".join(item.name for item in chain)
                child.parent = parent.path
                children[child.path] = child
                directories[child.path] = child
                for member in chain:
                    aliases[member.path] = child.path
                stack.append(child)
            parent.directories = children
            parent.files.sort(key=lambda path: (path.rpartition("/")[2].casefold(), path))
        self._directories = directories
        self._aliases = aliases
        self._file_parents = {path: aliases[parent] for path, parent in file_parents.items()}
        self.expanded.intersection_update(directories)
        self._tree_selected = None

    def _set_rows(self, rows: tuple[ExplorerRow, ...], selected: str | None = None) -> None:
        self.rows = rows
        self._row_indices = {row.id: index for index, row in enumerate(rows)}
        self.set_options(
            Option(ExplorerLabel(row, expanded=row.path in self.expanded, search=self.search_mode), id=row.id)
            for row in rows
        )
        self.highlighted = self._row_indices.get(selected, 0) if rows else None
        self.scroll_to_highlight()
        self._update_tooltip()

    def _render_tree(self, selected: str | None = None) -> None:
        root = self._directories.get("")
        if root is None:
            self._set_rows(())
            return
        rows = []
        stack = [(root, -1)]
        while stack:
            node, depth = stack.pop()
            if isinstance(node, ExplorerRow):
                rows.append(node)
                continue
            if depth >= 0:
                rows.append(ExplorerRow(node.path, node.name, True, depth, node.parent))
                if node.path not in self.expanded:
                    continue
            children = [
                (child, depth + 1) for child in node.directories.values()
            ]
            # File rows follow every directory subtree.
            for path in reversed(node.files):
                stack.append((ExplorerRow(path, path.rpartition("/")[2], False, depth + 1, node.path), -2))
            stack.extend(reversed(children))
        self._set_rows(tuple(rows), selected)

    def show_search(self, paths: tuple[str, ...], selected: str | None = None) -> None:
        if not self.search_mode:
            row = self.selected_row
            self._tree_selected = row.id if row else None
        self.search_mode = True
        self._set_rows(tuple(
            ExplorerRow(path, path.rpartition("/")[2], parent=self._file_parents.get(path, ""))
            for path in paths if path in self._file_parents
        ), selected)

    def show_tree(self, path: str | None = None) -> None:
        row = self.selected_row
        selected = path or (row.id if row else self._tree_selected)
        self.search_mode = False
        if selected in self._file_parents:
            self._expand_parents(selected)
        elif selected and selected.removesuffix("/") in self._aliases:
            selected = self._aliases[selected.removesuffix("/")] + "/"
        self._render_tree(selected)

    def _expand_parents(self, path: str) -> None:
        parent = self._file_parents[path]
        while parent:
            self.expanded.add(parent)
            parent = self._directories[parent].parent

    def reveal_path(self, path: str) -> None:
        if self.search_mode or path not in self._file_parents:
            return
        parent = self._file_parents[path]
        needed = []
        while parent:
            if parent not in self.expanded:
                needed.append(parent)
            parent = self._directories[parent].parent
        if needed or path not in self._row_indices:
            self.expanded.update(needed)
            self._render_tree(path)
        else:
            self.highlighted = self._row_indices[path]
            self.scroll_to_highlight()

    def toggle_directory(self, path: str) -> None:
        if self.search_mode or path not in self._directories or not path:
            return
        if path in self.expanded:
            self.expanded.remove(path)
        else:
            self.expanded.add(path)
        self._render_tree(path + "/")

    def expand_selected(self) -> None:
        row = self.selected_row
        if self.search_mode or row is None or not row.directory:
            return
        if row.path not in self.expanded:
            self.toggle_directory(row.path)
        elif self.highlighted is not None and self.highlighted + 1 < len(self.rows):
            self.highlighted += 1

    def collapse_or_parent(self) -> None:
        row = self.selected_row
        if self.search_mode or row is None:
            return
        if row.directory and row.path in self.expanded:
            self.toggle_directory(row.path)
        elif row.parent:
            self.highlighted = self._row_indices.get(row.parent + "/", self.highlighted)
            self.scroll_to_highlight()

    def _update_tooltip(self, index: int | None = None) -> None:
        row = self.row_at(self.highlighted if index is None else index)
        self.tooltip = display_path(row.path) + ("/" if row.directory else "") if row else None

    def watch_highlighted(self, highlighted: int | None) -> None:
        super().watch_highlighted(highlighted)
        self._update_tooltip()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        self._update_tooltip(event.style.meta.get("option"))

    def on_leave(self) -> None:
        self._update_tooltip()

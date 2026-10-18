from __future__ import annotations

import asyncio
from dataclasses import dataclass

from rich.syntax import Syntax
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, Label, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from .git import LineRange, Repository, Snapshot, Timeline, TrailError, display_source
from .selectors import enclosing_symbol, paragraph, symbols


class SourceView(TextArea):
    BINDINGS = [
        Binding("enter", "trace", "Trace selection", priority=True),
        Binding("ctrl+p", "paragraph", "Paragraph", priority=True),
        Binding("ctrl+s", "symbol", "Containing symbol", priority=True),
    ]

    def action_trace(self) -> None:
        self.app.action_trace()

    def action_paragraph(self) -> None:
        self.app.action_paragraph()

    def action_symbol(self) -> None:
        self.app.action_symbol()


class JumpScreen(ModalScreen[int | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, line_count: int):
        super().__init__()
        self.line_count = line_count

    def compose(self) -> ComposeResult:
        with Vertical(id="jump-dialog"):
            yield Label(f"Go to a line (1 to {self.line_count})")
            yield Input(placeholder="Line number", type="integer", id="jump-input")
            yield Static("Enter to jump · Escape to cancel", id="jump-message")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        try:
            line = int(event.value)
        except ValueError:
            line = 0
        if not 1 <= line <= self.line_count:
            self.query_one("#jump-message", Static).update(f"Enter a number from 1 to {self.line_count}.")
            return
        self.dismiss(line)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SymbolScreen(ModalScreen[LineRange | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, snapshot: Snapshot):
        super().__init__()
        self.items = symbols(snapshot.text, snapshot.path)
        if not self.items:
            raise TrailError("No named functions or classes were found in this file.")

    def compose(self) -> ComposeResult:
        with Vertical(id="symbol-dialog"):
            yield Label("Select a function or class")
            yield OptionList(
                *[
                    Option(
                        Text(f"{item.kind:8} {item.name}  ·  {item.start}:{item.end}"),
                        id=str(index),
                    )
                    for index, item in enumerate(self.items)
                ],
                id="symbol-options",
            )
            yield Static("↑/↓ then Enter · Escape to cancel")

    def on_mount(self) -> None:
        options = self.query_one(OptionList)
        options.highlighted = 0
        options.focus()

    @on(OptionList.OptionSelected)
    def selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.items[event.option_index].selection)

    def action_cancel(self) -> None:
        self.dismiss(None)


@dataclass(frozen=True)
class InitialSelection:
    path: str | None = None
    selection: LineRange | None = None


class TrailApp(App):
    TITLE = "Git Trail"
    SUB_TITLE = "Read the story behind your source"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { background: #101820; color: #dce6ef; }
    Header { background: #1b2c3c; }
    Footer { background: #1b2c3c; }
    #body { height: 1fr; }
    #files-pane { width: 28; min-width: 18; border-right: solid #294054; }
    #files-title, #source-title, #timeline-title, #diff-title {
        height: 2; padding: 0 1; background: #1b2c3c; color: #8fdcc1;
    }
    #file-filter { margin: 0 1; height: 3; }
    #files { height: 1fr; border: none; background: #101820; padding: 0 1; }
    #workspace { width: 1fr; }
    #source-pane { height: 1fr; min-height: 8; }
    #source { height: 1fr; border: none; background: #101820; }
    #source:focus { border: tall #62bfa5; }
    #history-pane { height: 1fr; min-height: 8; border-top: solid #294054; }
    #timeline-pane { width: 38%; min-width: 22; border-right: solid #294054; }
    #timeline { height: 1fr; border: none; background: #101820; padding: 0 1; }
    #diff-pane { width: 1fr; }
    #diff-scroll { height: 1fr; padding: 0 1; overflow-x: auto; }
    #diff { width: auto; min-width: 100%; height: auto; }
    #status { height: 2; padding: 0 1; color: #9cafbf; background: #152331; }
    JumpScreen, SymbolScreen { align: center middle; background: #000000 65%; }
    #jump-dialog {
        width: 54; height: auto; padding: 1 2; border: thick #62bfa5;
        background: #152331;
    }
    #jump-input { margin: 1 0; }
    #jump-message { height: auto; }
    #symbol-dialog {
        width: 80%; max-width: 100; height: 75%; padding: 1 2;
        border: thick #62bfa5; background: #152331;
    }
    #symbol-options { height: 1fr; margin: 1 0; }
    """
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("ctrl+f", "find_file", "Files", priority=True),
        Binding("ctrl+g", "jump", "Go to line", priority=True),
        Binding("ctrl+t", "trace", "Trace", priority=True),
        Binding("ctrl+b", "browse_symbols", "Functions/classes", priority=True),
        Binding("ctrl+p", "paragraph", "Paragraph", priority=True),
        Binding("ctrl+s", "symbol", "Containing symbol", priority=True),
        Binding("ctrl+j", "older", "Older", priority=True),
        Binding("ctrl+k", "newer", "Newer", priority=True),
    ]

    def __init__(self, repository: Repository, initial: InitialSelection | None = None):
        super().__init__()
        self.repository = repository
        self.initial = initial or InitialSelection()
        self.snapshot: Snapshot | None = None
        self.timeline: Timeline | None = None
        self.filtered_files: tuple[str, ...] = ()
        self.all_files: tuple[str, ...] = ()
        self._load_generation = 0
        self._history_generation = 0

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            with Vertical(id="files-pane"):
                yield Static("FILES AT STARTING COMMIT", id="files-title")
                yield Input(placeholder="Filter files…", id="file-filter")
                yield OptionList(id="files", markup=False)
            with Vertical(id="workspace"):
                with Vertical(id="source-pane"):
                    yield Static("SOURCE · choose a file", id="source-title")
                    yield SourceView(
                        read_only=True, show_line_numbers=True, soft_wrap=False,
                        id="source", tab_behavior="focus",
                    )
                with Horizontal(id="history-pane"):
                    with Vertical(id="timeline-pane"):
                        yield Static("TIMELINE", id="timeline-title")
                        yield OptionList(id="timeline", markup=False)
                    with Vertical(id="diff-pane"):
                        yield Static("SELECTED-RANGE DIFF", id="diff-title")
                        with VerticalScroll(id="diff-scroll"):
                            yield Static(
                                "Select source text with Shift+arrows or the mouse, then press Enter.\n"
                                "Ctrl+P selects a paragraph. Ctrl+B lists functions and classes.",
                                id="diff", markup=False,
                            )
        yield Static("Reading repository…", id="status", markup=False)
        yield Footer()

    async def on_mount(self) -> None:
        self.sub_title = f"{self.repository.root.name} · {self.repository.commit[:10]}"
        try:
            self.all_files = await asyncio.to_thread(self.repository.files)
        except TrailError as error:
            self.status(str(error), error=True)
            return
        self.filter_files("")
        if self.initial.path:
            await self.load_file(self.initial.path, self.initial.selection)
        else:
            self.status(
                "Choose a file. All source and selections use the starting commit, HEAD by default."
            )
            self.query_one("#file-filter", Input).focus()

    def status(self, message: str, error: bool = False) -> None:
        self.query_one("#status", Static).update(Text(message, style="#f49b9b" if error else "#9cafbf"))

    def filter_files(self, query: str) -> None:
        self.filtered_files = tuple(path for path in self.all_files if query.casefold() in path.casefold())
        options = self.query_one("#files", OptionList)
        options.clear_options()
        options.add_options(
            Option(Text(path), id=str(index))
            for index, path in enumerate(self.filtered_files)
        )
        options.highlighted = 0 if self.filtered_files else None

    @on(Input.Changed, "#file-filter")
    def file_filter_changed(self, event: Input.Changed) -> None:
        self.filter_files(event.value)

    @on(Input.Submitted, "#file-filter")
    def file_filter_submitted(self) -> None:
        options = self.query_one("#files", OptionList)
        if self.filtered_files and options.highlighted is not None:
            self.open_file(self.filtered_files[options.highlighted])

    @on(OptionList.OptionSelected, "#files")
    def file_selected(self, event: OptionList.OptionSelected) -> None:
        self.open_file(self.filtered_files[event.option_index])

    def open_file(self, path: str) -> None:
        self.run_worker(self.load_file(path), group="file", exclusive=True, exit_on_error=False)

    async def load_file(self, path: str, selection: LineRange | None = None) -> None:
        self._load_generation += 1
        generation = self._load_generation
        self._history_generation += 1
        self.snapshot = None
        self.timeline = None
        self.query_one("#timeline", OptionList).clear_options()
        self.query_one("#timeline-title", Static).update("TIMELINE")
        self.query_one("#diff-title", Static).update("SELECTED-RANGE DIFF")
        self.query_one("#source", SourceView).load_text("")
        self.query_one("#diff", Static).update("Choose a selection in the committed source.")
        self.query_one("#source-title", Static).update(Text(f"LOADING · {path}"))
        self.status(f"Loading {path}…")
        try:
            snapshot = await asyncio.to_thread(self.repository.snapshot, path)
        except TrailError as error:
            if generation == self._load_generation:
                self.status(str(error), error=True)
            return
        if generation != self._load_generation:
            return
        self.snapshot = snapshot
        source = self.query_one("#source", SourceView)
        source.load_text(display_source(snapshot.text))
        self.query_one("#source-title", Static).update(
            Text(f"{snapshot.path} · committed {snapshot.commit[:10]}")
        )
        source.focus()
        message = "Shift+arrows selects a range · Enter traces · Ctrl+B lists symbols"
        if snapshot.working_tree_changed:
            message = "Local edits exist. Showing committed source. " + message
        self.status(message)
        if selection:
            self.select_range(selection)
            await self.trace_selection(selection)

    def selected_range(self) -> LineRange:
        if self.snapshot is None:
            raise TrailError("Choose a file first.")
        source = self.query_one("#source", SourceView)
        start, end = sorted((source.selection.start, source.selection.end))
        start_row = min(start[0], len(self.snapshot.lines) - 1)
        last_row = end[0]
        if end[0] > start[0] and end[1] == 0:
            last_row -= 1
        selection = LineRange(
            start_row + 1,
            min(max(start_row, last_row) + 1, len(self.snapshot.lines)),
            "source selection",
        )
        selection.validate(len(self.snapshot.lines))
        return selection

    def select_range(self, selection: LineRange) -> None:
        if self.snapshot is None:
            raise TrailError("Choose a file first.")
        selection.validate(len(self.snapshot.lines))
        source = self.query_one("#source", SourceView)
        source.move_cursor((selection.start - 1, 0), center=True)
        source.move_cursor(
            (selection.end - 1, len(source.document.get_line(selection.end - 1))),
            select=True,
        )
        self.status(
            f"Selected {selection.label or 'lines'} · {selection.start}:{selection.end} · Enter to trace"
        )

    def action_trace(self) -> None:
        try:
            selection = self.selected_range()
        except TrailError as error:
            self.status(str(error), error=True)
            return
        self.run_worker(
            self.trace_selection(selection), group="history", exclusive=True, exit_on_error=False
        )

    async def trace_selection(self, selection: LineRange) -> None:
        if self.snapshot is None:
            return
        snapshot = self.snapshot
        self._history_generation += 1
        generation = self._history_generation
        self.timeline = None
        self.query_one("#timeline", OptionList).clear_options()
        self.query_one("#diff", Static).update("Tracing selected lines…")
        self.status(f"Tracing {snapshot.path}:{selection.start}:{selection.end}…")
        try:
            timeline = await asyncio.to_thread(self.repository.history, snapshot, selection)
        except TrailError as error:
            if generation == self._history_generation:
                self.query_one("#diff", Static).update("History unavailable.")
                self.status(str(error), error=True)
            return
        if generation != self._history_generation or self.snapshot != snapshot:
            return
        self.timeline = timeline
        options = self.query_one("#timeline", OptionList)
        options.clear_options()
        options.add_options(
            Option(
                Text(
                    f"{entry.commit[:8]}  {entry.committed_at[:10]}\n"
                    f"{entry.subject}\n{entry.author}",
                ),
                id=str(index),
            )
            for index, entry in enumerate(timeline.entries)
        )
        self.query_one("#timeline-title", Static).update(
            f"TIMELINE · {len(timeline.entries)} changes · {selection.start}:{selection.end}"
        )
        if timeline.entries:
            options.highlighted = 0
            self.show_entry(0)
            options.focus()
        else:
            self.query_one("#diff", Static).update("No changes were returned for this selection.")
        self.status(
            f"{len(timeline.entries)} changes · Ctrl+J older / Ctrl+K newer · "
            "textual history; moves and all merge-parent paths are not guaranteed"
            + (" · shallow clone: older history may be missing" if any("shallow" in warning for warning in timeline.warnings) else "")
            + (" · local edits excluded" if snapshot.working_tree_changed else "")
        )

    @on(OptionList.OptionHighlighted, "#timeline")
    def entry_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self.show_entry(event.option_index)

    def show_entry(self, index: int) -> None:
        if self.timeline is None or not 0 <= index < len(self.timeline.entries):
            return
        entry = self.timeline.entries[index]
        self.query_one("#diff-title", Static).update(
            Text(f"{entry.commit[:10]} · {entry.author} · committed {entry.committed_at}")
        )
        self.query_one("#diff", Static).update(
            Syntax(
                display_source(entry.patch), "diff", theme="monokai",
                word_wrap=False, background_color="#101820",
            )
        )
        self.query_one("#diff-scroll", VerticalScroll).scroll_home(animate=False)

    def navigate(self, offset: int) -> None:
        options = self.query_one("#timeline", OptionList)
        if self.timeline is None or not self.timeline.entries:
            return
        index = min(max((options.highlighted or 0) + offset, 0), len(self.timeline.entries) - 1)
        options.highlighted = index
        self.show_entry(index)

    def action_older(self) -> None:
        self.navigate(1)

    def action_newer(self) -> None:
        self.navigate(-1)

    def action_find_file(self) -> None:
        self.query_one("#file-filter", Input).focus()

    def action_jump(self) -> None:
        if self.snapshot is None:
            self.status("Choose a file first.", error=True)
            return
        self.push_screen(JumpScreen(len(self.snapshot.lines)), self.jump_to)

    def jump_to(self, line: int | None) -> None:
        if line is not None and self.snapshot:
            self.query_one("#source", SourceView).move_cursor((line - 1, 0), center=True)
            self.query_one("#source", SourceView).focus()

    def action_paragraph(self) -> None:
        if self.snapshot is None:
            self.status("Choose a file first.", error=True)
            return
        try:
            line = self.query_one("#source", SourceView).cursor_location[0] + 1
            self.select_range(paragraph(self.snapshot.text, line))
        except TrailError as error:
            self.status(str(error), error=True)

    def action_symbol(self) -> None:
        if self.snapshot is None:
            self.status("Choose a file first.", error=True)
            return
        try:
            line = self.query_one("#source", SourceView).cursor_location[0] + 1
            self.select_range(enclosing_symbol(self.snapshot.text, self.snapshot.path, line))
        except TrailError as error:
            self.status(str(error), error=True)

    def action_browse_symbols(self) -> None:
        if self.snapshot is None:
            self.status("Choose a file first.", error=True)
            return
        try:
            screen = SymbolScreen(self.snapshot)
        except TrailError as error:
            self.status(str(error), error=True)
            return
        self.push_screen(screen, self.symbol_chosen)

    def symbol_chosen(self, selection: LineRange | None) -> None:
        if selection is not None:
            self.select_range(selection)
            self.query_one("#source", SourceView).focus()

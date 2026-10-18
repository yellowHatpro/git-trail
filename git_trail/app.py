from __future__ import annotations

import asyncio
from dataclasses import dataclass
from importlib.resources import files as resource_files
from pathlib import PurePosixPath

from pygments.style import Style as PygmentsStyle
from pygments.token import Generic, Token
from rich.syntax import PygmentsSyntaxTheme, Syntax
from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Footer, Header, Input, Label, OptionList, Static, TabbedContent, TabPane, TextArea
from textual.widgets.option_list import Option
from tree_sitter import QueryError
from tree_sitter_language_pack import get_language

from .filters import HistoryFilter, filter_files, find_matches
from .git import HistoryEntry, LineRange, Repository, Snapshot, Timeline, TrailError, display_source
from .selectors import enclosing_symbol, paragraph, symbols
from .widgets import PaneDivider


class DiffStyle(PygmentsStyle):
    background_color = "#10182b"
    styles = {
        Token: "#dbe4f0",
        Generic.Deleted: "#fda4af",
        Generic.Inserted: "#86efac",
        Generic.Heading: "bold #a5b4fc",
        Generic.Subheading: "#67e8f9",
    }


DIFF_THEME = PygmentsSyntaxTheme(DiffStyle)


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

    def configure_language(self, language: str | None) -> None:
        if language is None:
            self.language = None
            return
        try:
            query = resource_files("textual").joinpath(
                "tree-sitter", "highlights", f"{language}.scm",
            ).read_text()
            self.register_language(language, get_language(language), query)
            self.language = language
        except (ImportError, LookupError, OSError, ValueError, QueryError):
            self.language = None


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
        self.filtered_items = self.items

    def compose(self) -> ComposeResult:
        with Vertical(id="symbol-dialog"):
            yield Label("Select a function or class")
            yield Input(placeholder="Find a function or class…", id="symbol-filter")
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
        self.query_one(Input).focus()

    @on(Input.Changed, "#symbol-filter")
    def filter_changed(self, event: Input.Changed) -> None:
        query = event.value.casefold()
        self.filtered_items = tuple(item for item in self.items if query in item.name.casefold())
        options = self.query_one("#symbol-options", OptionList)
        options.clear_options()
        options.add_options(
            Option(Text(f"{item.kind:8} {item.name} · {item.start}:{item.end}"), id=str(index))
            for index, item in enumerate(self.filtered_items)
        )
        options.highlighted = 0 if self.filtered_items else None

    @on(Input.Submitted, "#symbol-filter")
    def filter_submitted(self) -> None:
        index = self.query_one("#symbol-options", OptionList).highlighted
        if index is not None:
            self.dismiss(self.filtered_items[index].selection)

    @on(OptionList.OptionSelected)
    def selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.filtered_items[event.option_index].selection)

    def action_cancel(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss", "Close"), Binding("f1", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-dialog"):
            yield Static(Text.from_markup(
                "[bold #a5b4fc]GIT TRAIL  /  KEYBOARD & FILTERS[/]\n\n"
                "[bold]Explore[/]\n"
                "Ctrl+O  Find a file     Ctrl+B  Find a function/class\n"
                "Ctrl+G  Go to line      Ctrl+F  Find in displayed code\n"
                "F3 / Shift+F3  Next / previous match\n"
                "Shift+arrows  Select lines     Enter / Ctrl+T  Trace\n"
                "Ctrl+P  Paragraph      Ctrl+S  Containing symbol\n\n"
                "[bold]History[/]\n"
                "Ctrl+J / Ctrl+K  Older / newer change\n"
                "Ctrl+L  Filter history   Ctrl+D  Diff / commit details\n"
                "F4  Cycle After / Before / Starting source\n"
                "Ctrl+Y  Copy selected commit SHA\n\n"
                "[bold]Filters[/]\n"
                'Files: fuzzy words, ext:py, ext:ts. Example: app ext:py\n'
                'History: words match subject, author, SHA, or patch.\n'
                'author:"Jane Doe"  hash:a1b2  after:2026-01-01  before:2026-12-31\n'
                "Combine terms to narrow results. Dates include both endpoints.\n"
                "Dates use each commit's recorded calendar date.\n\n"
                "[bold]Layout[/]\n"
                "Drag the sidebar divider, or focus it and press ← / →.\n"
                "Ctrl+Shift+← / →  Resize sidebar    F2  Hide / show files\n"
                "Mouse wheel / PageUp / PageDown / Home / End  Scroll lists\n"
                "Tab / Shift+Tab  Change pane    Ctrl+Q  Quit\n\n"
                "[dim]Committed source only. Cross-file moves and every merge-parent\n"
                "path are not guaranteed. Before a merge shows its first parent.\n"
                "Escape closes this guide.[/]"
            ))


@dataclass(frozen=True)
class InitialSelection:
    path: str | None = None
    selection: LineRange | None = None


class TrailApp(App):
    TITLE = "Git Trail"
    SUB_TITLE = "Source · History · Context"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { background: #0b1020; color: #dbe4f0; }
    Header { background: #151d33; color: #c7d2fe; }
    Footer { background: #151d33; color: #94a3b8; }
    #body { height: 1fr; padding: 1 1 0 1; }
    #files-pane { width: 30; min-width: 16; background: #10182b; border: round #25324c; }
    #sidebar-divider { width: 1; color: #3d4d72; }
    #sidebar-divider:hover, #sidebar-divider:focus { color: #a5b4fc; background: #1f2b4c; }
    #files-title, #source-title, #timeline-title, #diff-title {
        height: 1; padding: 0 1; color: #a5b4fc; text-style: bold;
    }
    #file-filter { margin: 0 1; height: 3; }
    Input { border: tall #334361; background: #131e34; }
    Input:focus { border: tall #818cf8; }
    #files { height: 1fr; min-height: 0; max-height: 100%; overflow-y: auto; scrollbar-size-vertical: 1; border: none; background: #10182b; padding: 0 1; text-wrap: nowrap; text-overflow: ellipsis; }
    #file-note { height: 1; padding: 0 1; color: #64748b; }
    OptionList > .option-list--option-highlighted { background: #243357; color: #e0e7ff; text-style: bold; }
    OptionList:focus > .option-list--option-highlighted { background: #373f78; color: #ffffff; }
    OptionList > .option-list--option-hover { background: #1c2945; }
    #workspace { width: 1fr; }
    #source-pane { height: 3fr; min-height: 6; border: round #25324c; background: #10182b; }
    #source-pane:focus-within { border: round #6573bd; }
    #source-toolbar { height: 1; margin: 0 1; }
    #source-toolbar Button { height: 1; min-width: 10; width: auto; padding: 0 1; border: none; background: #19253f; color: #94a3b8; }
    #source-toolbar Button.active { background: #373f78; color: #e0e7ff; text-style: bold; }
    #source-note { height: 1; color: #7e91b5; padding: 0 1; }
    #source { height: 1fr; min-height: 0; border: none; background: #10182b; scrollbar-size-vertical: 1; }
    #source:focus { border: none; }
    #find-bar { height: 3; margin: 0 1; }
    #find-input { width: 1fr; }
    #find-bar Button { min-width: 3; width: 3; height: 3; padding: 0; }
    #find-bar Checkbox { width: 8; min-width: 8; height: 3; padding: 0; border: none; }
    #find-count { width: 9; content-align: center middle; color: #a5b4fc; }
    #history-pane { height: 2fr; min-height: 7; margin-top: 1; }
    #timeline-pane { width: 34%; min-width: 22; border: round #25324c; background: #10182b; }
    #history-filter { height: 3; margin: 0 1; }
    #timeline { height: 1fr; min-height: 0; overflow-y: auto; scrollbar-size-vertical: 1; border: none; background: #10182b; padding: 0 1; }
    #diff-pane { width: 1fr; }
    #review-tabs { height: 1fr; border: round #25324c; background: #10182b; }
    TabPane { padding: 0; }
    ContentSwitcher { height: 1fr; }
    Tabs { height: 2; }
    #diff-scroll, #commit-scroll { height: 1fr; padding: 0 1; overflow-x: auto; scrollbar-size-vertical: 1; }
    #diff { width: auto; min-width: 100%; height: auto; }
    #commit-details { height: auto; }
    #status { height: 2; padding: 0 2; color: #8fa3c5; background: #0b1020; }
    JumpScreen, SymbolScreen, HelpScreen { align: center middle; background: #000000 65%; }
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
    #symbol-filter { height: 3; }
    #symbol-options { height: 1fr; margin: 1 0; }
    #help-dialog { width: 80; max-width: 95%; height: auto; max-height: 90%; padding: 1 3; border: round #818cf8; background: #131e34; }
    Screen.compact #body { padding: 0; }
    Screen.compact #history-pane { margin-top: 0; }
    Screen.compact #source-pane { height: 1fr; }
    Screen.compact #history-pane { height: 1fr; }
    Screen.compact #source-note { display: none; }
    Screen.compact #source-note.important { display: block; }
    Screen.compact #find-bar { height: 1; }
    Screen.compact #find-bar Input { height: 1; border: none; padding: 0; }
    Screen.compact #find-bar Button, Screen.compact #find-bar Checkbox { height: 1; border: none; padding: 0; }
    Screen.compact #find-count { width: 7; }
    Screen.compact #timeline { text-wrap: nowrap; text-overflow: ellipsis; }
    """
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("ctrl+o", "find_file", "Files", priority=True),
        Binding("ctrl+f", "find_code", "Find code", priority=True),
        Binding("f3", "next_match", "Next match", show=False, priority=True),
        Binding("shift+f3", "previous_match", "Previous match", show=False, priority=True),
        Binding("escape", "close_find", "Close find", show=False),
        Binding("ctrl+l", "find_history", "Filter", priority=True),
        Binding("ctrl+d", "details", "Details", priority=True),
        Binding("f4", "cycle_source", "Source view", show=False, priority=True),
        Binding("ctrl+y", "copy_sha", "Copy SHA", show=False, priority=True),
        Binding("ctrl+shift+left", "narrow_sidebar", "Narrow sidebar", show=False, priority=True),
        Binding("ctrl+shift+right", "widen_sidebar", "Widen sidebar", show=False, priority=True),
        Binding("f2", "toggle_sidebar", "Sidebar", show=False, priority=True),
        Binding("f1", "help", "Help", priority=True),
        Binding("ctrl+g", "jump", "Go to line", priority=True),
        Binding("ctrl+t", "trace", "Trace", priority=True),
        Binding("ctrl+b", "browse_symbols", "Functions/classes", priority=True),
        Binding("ctrl+p", "paragraph", "Paragraph", show=False, priority=True),
        Binding("ctrl+s", "symbol", "Containing symbol", show=False, priority=True),
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
        self._view_generation = 0
        self.snapshot_repository = repository
        self.traced_snapshot: Snapshot | None = None
        self.filtered_entries: tuple[HistoryEntry, ...] = ()
        self.selected_entry: HistoryEntry | None = None
        self.source_mode = "after"
        self.sidebar_width = 30
        self._sidebar_visible = True
        self._search_matches = ()
        self._match_index = -1
        self._rendering_timeline = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            with Vertical(id="files-pane"):
                yield Static("FILES", id="files-title")
                yield Input(placeholder="Find file · ext:py", id="file-filter")
                yield OptionList(id="files", markup=False)
                yield Static("Ctrl+O  ·  drag edge to resize", id="file-note")
            yield PaneDivider(id="sidebar-divider", tooltip="Drag to resize. Left/Right when focused.")
            with Vertical(id="workspace"):
                with Vertical(id="source-pane"):
                    yield Static("SOURCE · choose a file", id="source-title")
                    with Horizontal(id="source-toolbar"):
                        yield Button("After", id="view-after", classes="active", compact=True)
                        yield Button("Before", id="view-before", compact=True)
                        yield Button("Starting", id="view-starting", compact=True)
                    yield Static("Committed source · Ctrl+F to find", id="source-note")
                    with Horizontal(id="find-bar"):
                        yield Input(placeholder="Find text or function name…", id="find-input")
                        yield Checkbox("Aa", id="find-case", tooltip="Match case")
                        yield Checkbox(".*", id="find-regex", tooltip="Regular expression")
                        yield Button("↑", id="find-previous", tooltip="Previous match (Shift+F3)")
                        yield Button("↓", id="find-next", tooltip="Next match (F3)")
                        yield Static("", id="find-count")
                    yield SourceView(
                        read_only=True, show_line_numbers=True, soft_wrap=False,
                        id="source", tab_behavior="focus", theme="dracula",
                    )
                with Horizontal(id="history-pane"):
                    with Vertical(id="timeline-pane"):
                        yield Static("TIMELINE", id="timeline-title")
                        yield Input(placeholder="Filter · author: / after:", id="history-filter")
                        yield OptionList(id="timeline", markup=False)
                    with Vertical(id="diff-pane"):
                        with TabbedContent(id="review-tabs"):
                            with TabPane("Diff", id="diff-tab"):
                                yield Static("SELECTED-RANGE DIFF", id="diff-title")
                                with VerticalScroll(id="diff-scroll"):
                                    yield Static(
                                        "Choose a file, select lines, and press Enter.\n\n"
                                        "Ctrl+B finds functions and classes.\n"
                                        "Ctrl+F searches the displayed source.\n"
                                        "F1 opens shortcuts and filter examples.",
                                        id="diff", markup=False,
                                    )
                            with TabPane("Commit", id="commit-tab"):
                                with VerticalScroll(id="commit-scroll"):
                                    yield Static("Trace a selection to explore commit context.", id="commit-details", markup=False)
        yield Static("Reading repository…", id="status", markup=False)
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#find-bar").display = False
        self.resize_sidebar(self.sidebar_width)
        self.screen.set_class(self.size.width < 100 or self.size.height < 30, "compact")
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
        try:
            filtered = filter_files(self.all_files, query)
        except TrailError as error:
            self.status(str(error), error=True)
            return
        self.filtered_files = filtered
        options = self.query_one("#files", OptionList)
        options.clear_options()
        options.add_options(
            Option(Text(path, no_wrap=True, overflow="ellipsis"), id=str(index))
            for index, path in enumerate(self.filtered_files)
        )
        options.highlighted = 0 if self.filtered_files else None
        self.query_one("#files-title", Static).update(f"FILES  {len(filtered)}/{len(self.all_files)}")

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
        self._view_generation += 1
        self.snapshot = None
        self.traced_snapshot = None
        self.snapshot_repository = self.repository
        self.timeline = None
        self.filtered_entries = ()
        self.selected_entry = None
        self.set_source_mode("after")
        self.query_one("#timeline", OptionList).clear_options()
        self.query_one("#timeline-title", Static).update("TIMELINE")
        self.query_one("#diff-title", Static).update("SELECTED-RANGE DIFF")
        self.query_one("#source", SourceView).load_text("")
        self.refresh_search()
        self.query_one("#commit-details", Static).update("Choose a file and trace its source.")
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
        self.traced_snapshot = snapshot
        source = self.query_one("#source", SourceView)
        self.display_snapshot(snapshot)
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
        repository = self.snapshot_repository
        self._history_generation += 1
        generation = self._history_generation
        self.timeline = None
        self._view_generation += 1
        self.traced_snapshot = snapshot
        self.filtered_entries = ()
        self.selected_entry = None
        self.query_one("#timeline", OptionList).clear_options()
        self.query_one("#diff-title", Static).update("SELECTED-RANGE DIFF")
        self.query_one("#commit-details", Static).update("Tracing selection…")
        self.query_one("#diff", Static).update("Tracing selected lines…")
        self.status(f"Tracing {snapshot.path}:{selection.start}:{selection.end}…")
        try:
            timeline = await asyncio.to_thread(repository.history, snapshot, selection)
        except TrailError as error:
            if generation == self._history_generation:
                self.query_one("#diff", Static).update("History unavailable.")
                self.status(str(error), error=True)
            return
        if generation != self._history_generation or self.snapshot != snapshot:
            return
        self.timeline = timeline
        self.apply_history_filter()
        if self.filtered_entries:
            self.query_one("#timeline", OptionList).focus()
        self.status(
            f"{len(timeline.entries)} changes · Ctrl+J/K navigate · F4 source views · F1 help"
            + (" · shallow history" if any("shallow" in warning for warning in timeline.warnings) else "")
            + (" · local edits excluded" if snapshot.working_tree_changed else "")
        )

    def apply_history_filter(self) -> None:
        if self.timeline is None:
            return
        try:
            predicate = HistoryFilter.parse(self.query_one("#history-filter", Input).value)
        except TrailError as error:
            self.status(str(error), error=True)
            return
        selected_sha = self.selected_entry.commit if self.selected_entry else ""
        self.filtered_entries = tuple(entry for entry in self.timeline.entries if predicate.matches(entry))
        self._view_generation += 1
        self._rendering_timeline = True
        options = self.query_one("#timeline", OptionList)
        options.clear_options()
        options.add_options(
            Option(
                Text(
                    f"{entry.commit[:7]} {entry.committed_at[:10]}\n"
                    f"{entry.subject}\n{entry.author}",
                ),
                id=entry.commit,
            )
            for entry in self.filtered_entries
        )
        self.query_one("#timeline-title", Static).update(
            f"TIMELINE  {len(self.filtered_entries)}/{len(self.timeline.entries)}"
        )
        if self.filtered_entries:
            index = next((index for index, entry in enumerate(self.filtered_entries) if entry.commit == selected_sha), 0)
            options.highlighted = index
            self._rendering_timeline = False
            self.show_entry(index)
        else:
            options.highlighted = None
            self._rendering_timeline = False
            self.selected_entry = None
            self.snapshot = None
            self.query_one("#diff", Static).update("No matching changes. Clear or adjust the history filter.")
            self.query_one("#diff-title", Static).update("NO MATCHING CHANGES")
            self.query_one("#commit-details", Static).update("No commit selected.")
            self.query_one("#source", SourceView).load_text("")
            self.query_one("#source-title", Static).update("SOURCE · no matching changes")
            self.query_one("#source-note", Static).update("Clear the history filter to return to this timeline.")
            self.refresh_search()

    @on(Input.Changed, "#history-filter")
    def history_filter_changed(self) -> None:
        self.apply_history_filter()

    @on(Input.Submitted, "#history-filter")
    def history_filter_submitted(self) -> None:
        self.query_one("#timeline", OptionList).focus()

    @on(OptionList.OptionHighlighted, "#timeline")
    def entry_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if not self._rendering_timeline and event.option.id in {entry.commit for entry in self.filtered_entries}:
            index = next(index for index, entry in enumerate(self.filtered_entries) if entry.commit == event.option.id)
            self.show_entry(index)

    def show_entry(self, index: int) -> None:
        if self.timeline is None or not 0 <= index < len(self.filtered_entries):
            return
        entry = self.filtered_entries[index]
        self.selected_entry = entry
        self.query_one("#diff-title", Static).update(
            Text(f"{entry.commit[:10]} · {entry.author} · committed {entry.committed_at}")
        )
        self.query_one("#diff", Static).update(
            Syntax(
                display_source(entry.patch), "diff", theme=DIFF_THEME,
                word_wrap=False, background_color="#10182b",
            )
        )
        self.call_after_refresh(self.position_diff)
        self._view_generation += 1
        generation = self._view_generation
        self.snapshot = None
        self.query_one("#source", SourceView).load_text("")
        self.query_one("#source-title", Static).update(Text(f"LOADING · {entry.commit[:10]}"))
        self.refresh_search()
        self.query_one("#commit-details", Static).update("Reading commit context…")
        self.query_one("#source-note", Static).update("Loading historical source…")
        self.query_one("#source-note", Static).add_class("important")
        self.run_worker(
            self.load_entry(entry, generation, self.source_mode),
            group="view", exclusive=True, exit_on_error=False,
        )

    async def load_entry(self, entry: HistoryEntry, generation: int, mode: str) -> None:
        try:
            if mode == "starting":
                snapshot = self.traced_snapshot
                selection = self.timeline.selection if self.timeline else None
                note = "Starting source for this timeline"
            else:
                view = await asyncio.to_thread(self.repository.historical_source, entry, mode == "before")
                snapshot, selection, note = view.snapshot, view.selection, view.note
            details = await asyncio.to_thread(self.repository.details, entry.commit)
            repository = await asyncio.to_thread(self.repository.at, snapshot.commit) if snapshot else self.repository
        except TrailError as error:
            if generation == self._view_generation:
                self.snapshot = None
                self.query_one("#source", SourceView).load_text("")
                self.query_one("#source-title", Static).update("SOURCE · unavailable")
                self.query_one("#source-note", Static).update(str(error))
                self.query_one("#source-note", Static).add_class("important")
                self.query_one("#commit-details", Static).update(str(error))
                self.refresh_search()
                self.status(str(error), error=True)
            return
        if generation != self._view_generation:
            return
        self.snapshot = snapshot
        self.snapshot_repository = repository
        if snapshot:
            self.display_snapshot(snapshot)
            if selection and snapshot.lines:
                self.select_range(selection)
        else:
            self.query_one("#source", SourceView).load_text("")
            self.query_one("#source-title", Static).update(f"SOURCE · {mode.upper()} · {entry.commit[:8]}")
        self.query_one("#source-note", Static).update(
            note or f"{mode.capitalize()} this change · Ctrl+F finds in this revision"
        )
        self.query_one("#source-note", Static).set_class(bool(note), "important")
        self.refresh_search()
        context = Text()
        context.append(f"{entry.commit}\n", style="bold #a5b4fc")
        context.append(f"{entry.author}\n", style="#e2e8f0")
        context.append(f"Committed  {entry.committed_at}\nAuthored   {entry.authored_at}\n\n", style="#94a3b8")
        context.append(details.message + "\n\n")
        context.append(f"CHANGED FILES  {len(details.files)}\n", style="bold #a5b4fc")
        for path in details.files:
            context.append(f"  {path}\n")
        if len(entry.parents) > 1:
            context.append("\nMerge commit. Files include changes against all parents.\n", style="#94a3b8")
        self.query_one("#commit-details", Static).update(context)
        self.status(
            f"{len(self.timeline.entries)} changes · {mode.capitalize()} {entry.commit[:8]} · Ctrl+J/K navigate · F1 help"
            + (" · local edits excluded" if self.traced_snapshot and self.traced_snapshot.working_tree_changed else "")
            + (" · shallow history" if self.timeline and any("shallow" in warning for warning in self.timeline.warnings) else "")
        )

    def navigate(self, offset: int) -> None:
        options = self.query_one("#timeline", OptionList)
        if self.timeline is None or not self.filtered_entries:
            return
        index = min(max((options.highlighted or 0) + offset, 0), len(self.filtered_entries) - 1)
        options.highlighted = index
        self.show_entry(index)

    def action_older(self) -> None:
        self.navigate(1)

    def action_newer(self) -> None:
        self.navigate(-1)

    def action_find_file(self) -> None:
        if not self._sidebar_visible:
            self.action_toggle_sidebar()
        self.query_one("#file-filter", Input).focus()

    def display_snapshot(self, snapshot: Snapshot) -> None:
        source = self.query_one("#source", SourceView)
        language = {
            ".py": "python", ".js": "javascript", ".go": "go", ".rs": "rust",
            ".java": "java", ".sh": "bash", ".json": "json", ".html": "html",
            ".css": "css", ".md": "markdown", ".toml": "toml", ".yaml": "yaml", ".yml": "yaml",
        }.get(PurePosixPath(snapshot.path).suffix.lower())
        source.configure_language(language)
        source.load_text(display_source(snapshot.text))
        self.query_one("#source-title", Static).update(Text(f"{snapshot.path}  ·  {snapshot.commit[:10]}"))
        self.query_one("#source-note", Static).update("Committed source · Ctrl+F to find")
        self.query_one("#source-note", Static).remove_class("important")
        self.refresh_search()

    def position_diff(self) -> None:
        if self.selected_entry is None:
            return
        line = 0
        if self.screen.has_class("compact"):
            line = next(
                (index for index, value in enumerate(self.selected_entry.patch.split("\n")) if value.startswith("@@ ")),
                0,
            )
        self.query_one("#diff-scroll", VerticalScroll).scroll_to(y=line, animate=False, immediate=True)

    @on(Button.Pressed, "#view-after, #view-before, #view-starting")
    def source_view_pressed(self, event: Button.Pressed) -> None:
        self.set_source_mode(event.button.id.removeprefix("view-"))

    def set_source_mode(self, mode: str) -> None:
        if self.selected_entry is None and mode != "after":
            self.status("Trace a selection before choosing a historical source view.")
            return
        self.source_mode = mode
        for value in ("after", "before", "starting"):
            self.query_one(f"#view-{value}", Button).set_class(value == mode, "active")
        if self.selected_entry:
            self.show_entry(self.filtered_entries.index(self.selected_entry))

    def action_cycle_source(self) -> None:
        modes = ("after", "before", "starting")
        self.set_source_mode(modes[(modes.index(self.source_mode) + 1) % 3])

    def action_details(self) -> None:
        tabs = self.query_one("#review-tabs", TabbedContent)
        tabs.active = "diff-tab" if tabs.active == "commit-tab" else "commit-tab"

    def action_copy_sha(self) -> None:
        if self.selected_entry:
            self.copy_to_clipboard(self.selected_entry.commit)
            self.status(f"Copied commit {self.selected_entry.commit[:10]} (terminal clipboard support required).")

    def action_find_history(self) -> None:
        self.query_one("#history-filter", Input).focus()

    def action_find_code(self) -> None:
        if self.snapshot is None:
            self.status("Choose a file or source revision first.", error=True)
            return
        self.query_one("#find-bar").display = True
        field = self.query_one("#find-input", Input)
        source = self.query_one("#source", SourceView)
        selected = source.selected_text
        if selected and "\n" not in selected:
            field.value = selected
        field.focus()
        self.refresh_search()

    def action_close_find(self) -> None:
        if self.query_one("#find-bar").display:
            self.query_one("#find-bar").display = False
            self.query_one("#source", SourceView).focus()

    @on(Input.Changed, "#find-input")
    @on(Checkbox.Changed, "#find-case, #find-regex")
    def find_changed(self) -> None:
        self.refresh_search()

    @on(Input.Submitted, "#find-input")
    def find_submitted(self) -> None:
        self.action_next_match()

    @on(Button.Pressed, "#find-next")
    def next_pressed(self) -> None:
        self.action_next_match()

    @on(Button.Pressed, "#find-previous")
    def previous_pressed(self) -> None:
        self.action_previous_match()

    def refresh_search(self) -> None:
        self._search_matches = ()
        self._match_index = -1
        field = self.query_one("#find-input", Input)
        if not field.value or not self.query_one("#find-bar").display:
            self.query_one("#find-count", Static).update("")
            return
        try:
            self._search_matches = find_matches(
                self.query_one("#source", SourceView).text, field.value,
                regex=self.query_one("#find-regex", Checkbox).value,
                case_sensitive=self.query_one("#find-case", Checkbox).value,
            )
        except TrailError as error:
            self.query_one("#find-count", Static).update("Invalid")
            self.status(str(error), error=True)
            return
        if self._search_matches:
            self.show_match(0)
        else:
            self.query_one("#find-count", Static).update("0 matches")

    def show_match(self, index: int) -> None:
        if not self._search_matches:
            return
        self._match_index = index % len(self._search_matches)
        start, end = self._search_matches[self._match_index]
        source = self.query_one("#source", SourceView)
        source.move_cursor(start, center=True)
        source.move_cursor(end, select=True)
        self.query_one("#find-count", Static).update(f"{self._match_index + 1}/{len(self._search_matches)}")

    def action_next_match(self) -> None:
        self.show_match(self._match_index + 1)

    def action_previous_match(self) -> None:
        self.show_match(self._match_index - 1)

    def resize_sidebar(self, width: int, *, available: int | None = None) -> None:
        if available is None:
            available = self.query_one("#body").size.width or self.size.width
        maximum = max(16, min(70, available - 55))
        self.sidebar_width = max(16, min(width, maximum))
        self.query_one("#files-pane").styles.width = self.sidebar_width

    def on_resize(self, event: events.Resize) -> None:
        if self.is_mounted:
            self.screen.set_class(event.size.width < 100 or event.size.height < 30, "compact")
            self.resize_sidebar(self.sidebar_width, available=event.size.width)
            self.call_after_refresh(self.position_diff)

    def action_narrow_sidebar(self) -> None:
        self.resize_sidebar(self.sidebar_width - 2)

    def action_widen_sidebar(self) -> None:
        self.resize_sidebar(self.sidebar_width + 2)

    def action_toggle_sidebar(self) -> None:
        self._sidebar_visible = not self._sidebar_visible
        self.query_one("#files-pane").display = self._sidebar_visible
        self.query_one("#sidebar-divider").display = self._sidebar_visible
        self.query_one("#source", SourceView).focus()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

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

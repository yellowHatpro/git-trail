from __future__ import annotations

import asyncio
import threading
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
from textual.widgets import Button, Checkbox, Input, Label, OptionList, Static, TabbedContent, TabPane, TextArea
from textual.widgets.option_list import Option
from tree_sitter import QueryError
from tree_sitter_language_pack import get_language

from .filters import HistoryFilter, filter_files, find_matches
from .git import HistoryEntry, LineRange, Repository, Snapshot, Timeline, TrailError, display_source
from .selectors import enclosing_symbol, paragraph, symbols
from .theme import BACKGROUND, EDITOR_THEME
from .state import DEFAULTS, NORMAL_ACTIONS, StateStore, validate_mapping
from .navigation import NavigationSupport, Visit, bookmark_name
from .diff import DiffPatch, SplitRow
from .diff_navigation import DiffNavigationSupport
from .vim import VimCursor
from .widgets import DiffContent, DiffScroll, PaneDivider


class DiffStyle(PygmentsStyle):
    background_color = BACKGROUND
    styles = {
        Token: "#c0caf5",
        Generic.Deleted: "#f7768e",
        Generic.Inserted: "#9ece6a",
        Generic.Heading: "#565f89",
        Generic.Subheading: "#7aa2f7",
    }


DIFF_THEME = PygmentsSyntaxTheme(DiffStyle)


class SourceView(TextArea):
    BINDINGS = [
        Binding("enter", "trace", "Trace selection", priority=True),
        Binding("ctrl+p", "paragraph", "Paragraph", priority=True),
        Binding("ctrl+s", "symbol", "Containing symbol", priority=True),
    ]

    def __init__(self, *args, **kwargs):
        self.vim = VimCursor()
        kwargs["theme"] = "css"
        super().__init__(*args, **kwargs)
        self.register_theme(EDITOR_THEME)
        self.theme = EDITOR_THEME.name

    def load_text(self, text: str) -> None:
        self.vim.reset()
        super().load_text(text)
        if self.is_mounted:
            self.app.set_editor_mode("NORMAL")

    def on_key(self, event: events.Key) -> None:
        if self.app.handle_normal_key(event):
            return
        if self.vim.handle(self, event.character or event.key):
            event.prevent_default()
            event.stop()

    def on_blur(self) -> None:
        self.vim.leave_visual(self, collapse=False)

    def action_trace(self) -> None:
        self.vim.leave_visual(self, collapse=False)
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
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+n,down", "next_result", "Next result", priority=True),
        Binding("ctrl+p,up", "previous_result", "Previous result", priority=True),
    ]

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

    def move_result(self, delta: int) -> None:
        options = self.query_one("#symbol-options", OptionList)
        if options.option_count:
            options.highlighted = max(0, min((options.highlighted or 0) + delta, options.option_count - 1))
            options.scroll_to_highlight()

    def action_next_result(self) -> None:
        self.move_result(1)

    def action_previous_result(self) -> None:
        self.move_result(-1)


class HelpScreen(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss", "Close"), Binding("f1", "dismiss", "Close")]

    def __init__(self, mappings: dict | None = None):
        super().__init__()
        self.mappings = mappings or {}

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-dialog"):
            if self.mappings:
                yield Static(Text("CUSTOM NORMAL KEYS\n" + "\n".join(f"{key}  {action}" for key, action in self.mappings.items()) + "\n"))
            yield Static(Text.from_markup(
                "[bold #a5b4fc]GIT TRAIL  /  KEYBOARD & FILTERS[/]\n\n"
                "[bold]Normal / Visual[/]\n"
                "h j k l  Move      w / b  Words      0 / $  Line ends\n"
                "gg / G  First / last line     3j / 42gg  Counts\n"
                "v / V  Character / line selection     Enter  Trace\n"
                "Escape  Normal mode     /  Find     n / N  Matches\n"
                "Ctrl+W then h j k l  Change pane     Ctrl+W w  Cycle\n"
                "Space f f / f s / f h  Files / symbols / history\n"
                "Space h b / h f / h m  Back / forward / bookmarks\n"
                ":42  Go to line     :help  Guide     :q  Quit\n"
                ":set ignorecase / noignorecase     :set regex / noregex\n"
                ":before / :after / :starting     :diff / :commit\n"
                ":file ext:py     :filter author:Jane     :symbols\n"
                ":follow  Track a whole symbol through refactors\n"
                ":bookmark NAME / :open NAME / :unbookmark NAME\n"
                ":back / :forward / :bookmarks     Alt+Left / Alt+Right\n"
                ":map KEY ACTION / :unmap KEY / :maps\n"
                "Ctrl+N/P or Up/Down  Picker results, Enter accepts\n\n"
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
    follow_symbol: bool = False
    resume: bool = True


class BookmarkScreen(ModalScreen[str | None]):
    BINDINGS = SymbolScreen.BINDINGS

    def __init__(self, bookmarks: dict):
        ModalScreen.__init__(self)
        self.items = tuple((bookmark_name(name), Visit.from_dict(value)) for name, value in bookmarks.items())
        self.filtered_items = self.items

    def compose(self) -> ComposeResult:
        with Vertical(id="symbol-dialog"):
            yield Label("Open a bookmark")
            yield Input(placeholder="Find a bookmark or file…", id="symbol-filter")
            yield OptionList(id="symbol-options")
            yield Static("Ctrl+N/P or ↑/↓ · Enter opens · Escape cancels")

    def on_mount(self) -> None:
        self.filter_changed(Input.Changed(self.query_one(Input), ""))
        self.query_one(Input).focus()

    @on(Input.Changed, "#symbol-filter")
    def filter_changed(self, event: Input.Changed) -> None:
        query = event.value.casefold()
        self.filtered_items = tuple(item for item in self.items if query in (item[0] + " " + item[1].path).casefold())
        options = self.query_one("#symbol-options", OptionList)
        options.clear_options()
        options.add_options(
            Option(Text(f"{name}\n{visit.path}:{visit.start}:{visit.end} · {visit.commit[:10]}"), id=str(index))
            for index, (name, visit) in enumerate(self.filtered_items)
        )
        options.highlighted = 0 if self.filtered_items else None

    @on(Input.Submitted, "#symbol-filter")
    def filter_submitted(self) -> None:
        index = self.query_one("#symbol-options", OptionList).highlighted
        if index is not None:
            self.dismiss(self.filtered_items[index][0])

    @on(OptionList.OptionSelected)
    def selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.filtered_items[event.option_index][0])

    def action_cancel(self) -> None:
        self.dismiss(None)

    def move_result(self, delta: int) -> None:
        options = self.query_one("#symbol-options", OptionList)
        if options.option_count:
            options.highlighted = max(0, min((options.highlighted or 0) + delta, options.option_count - 1))
            options.scroll_to_highlight()

    def action_next_result(self) -> None:
        self.move_result(1)

    def action_previous_result(self) -> None:
        self.move_result(-1)


class TrailApp(DiffNavigationSupport, NavigationSupport, App):
    TITLE = "Git Trail"
    SUB_TITLE = "Source · History · Context"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { background: #1a1b26; color: #c0caf5; }
    * {
        scrollbar-background: #16161e; scrollbar-background-hover: #16161e;
        scrollbar-background-active: #16161e; scrollbar-color: #3b4261;
        scrollbar-color-hover: #565f89; scrollbar-color-active: #7aa2f7;
        scrollbar-corner-color: #1a1b26; scrollbar-size-horizontal: 1;
    }
    #workspace-title { height: 1; padding: 0 1; background: #16161e; color: #7aa2f7; text-style: bold; }
    #body { height: 1fr; }
    #files-pane { width: 24; min-width: 16; background: #16161e; }
    #sidebar-divider { width: 1; color: #292e42; }
    #sidebar-divider:hover, #sidebar-divider:focus { color: #7aa2f7; }
    #files-title, #source-title, #timeline-title, #diff-title {
        height: 1; padding: 0 1; color: #a9b1d6; background: #24283b; text-wrap: nowrap; text-overflow: ellipsis;
    }
    #files-pane:focus-within #files-title, #source-pane:focus-within #source-title,
    #timeline-pane:focus-within #timeline-title, #diff-pane:focus-within #diff-title { color: #7aa2f7; }
    Input, Input:focus { height: 1; border: none; padding: 0; background: #16161e; color: #c0caf5; }
    Input > .input--placeholder { color: #565f89; }
    Input > .input--cursor { background: #c0caf5; color: #1a1b26; }
    #files { height: 1fr; min-height: 0; max-height: 100%; overflow-y: auto; scrollbar-size-vertical: 1; border: none; background: #16161e; padding: 0 1; text-wrap: nowrap; text-overflow: ellipsis; }
    OptionList > .option-list--option-highlighted { background: #24283b; color: #c0caf5; }
    OptionList:focus > .option-list--option-highlighted { background: #283457; color: #7aa2f7; }
    OptionList > .option-list--option-hover { background: #24283b; }
    #workspace { width: 1fr; }
    #source-pane { height: 3fr; min-height: 6; background: #1a1b26; }
    #source-heading { height: 1; background: #24283b; }
    #source-title { width: 1fr; }
    #source-toolbar { height: 1; width: 26; }
    #source-toolbar Button { height: 1; min-width: 6; width: auto; padding: 0 1; border: none; background: #24283b; color: #565f89; }
    #source-toolbar Button.active { color: #7aa2f7; text-style: bold; }
    #source-note { display: none; height: 1; color: #e0af68; padding: 0 1; }
    #source-note.important { display: block; }
    #source { height: 1fr; min-height: 0; border: none; background: #1a1b26; scrollbar-size-vertical: 1; }
    #source:focus { border: none; }
    #prompt-row { height: 1; background: #16161e; padding: 0 1; }
    #prompt-label { height: 1; width: auto; min-width: 2; color: #7aa2f7; }
    #file-filter, #history-filter, #command-input { height: 1; width: 1fr; }
    #find-bar { height: 1; width: 1fr; }
    #find-input { width: 1fr; }
    #find-bar Button { min-width: 3; width: 3; height: 1; padding: 0; border: none; background: #16161e; color: #7aa2f7; }
    #find-bar Checkbox { width: 8; min-width: 8; height: 1; padding: 0; border: none; background: #16161e; }
    #find-count { width: 9; content-align: center middle; color: #a5b4fc; }
    #find-case, #find-regex, #find-next, #find-previous { display: none; }
    #history-pane { height: 2fr; min-height: 6; border-top: solid #292e42; }
    #timeline-pane { width: 34%; min-width: 22; background: #1a1b26; border-right: solid #292e42; }
    #timeline { height: 1fr; min-height: 0; overflow-y: auto; scrollbar-size-vertical: 1; border: none; background: #1a1b26; padding: 0 1; }
    #diff-pane { width: 1fr; }
    #diff-heading { height: 1; background: #24283b; }
    #diff-title { width: 1fr; }
    #diff-layout { height: 1; width: 11; min-width: 11; border: none; padding: 0 1; background: #24283b; color: #7aa2f7; }
    #review-tabs { height: 1fr; background: #1a1b26; }
    TabPane { padding: 0; }
    ContentSwitcher { height: 1fr; }
    Tabs { height: 1; background: #24283b; }
    Tabs > .tabs-list { height: 1; }
    Tab { padding: 0 1; height: 1; color: #565f89; }
    Tab.-active { color: #7aa2f7; background: #24283b; }
    Underline { display: none; }
    #diff-scroll, #commit-scroll { height: 1fr; padding: 0 1; overflow-x: auto; scrollbar-size-vertical: 1; }
    #diff { width: auto; min-width: 100%; height: auto; }
    #commit-details { height: auto; }
    #statusline { height: 1; background: #24283b; }
    #mode { width: 9; content-align: center middle; background: #7aa2f7; color: #1a1b26; text-style: bold; }
    #mode.visual { background: #bb9af7; }
    #mode.prompt { background: #9ece6a; }
    #status { height: 1; width: 1fr; padding: 0 1; color: #a9b1d6; text-wrap: nowrap; text-overflow: ellipsis; }
    #position { width: 14; height: 1; color: #565f89; content-align: right middle; padding: 0 1; }
    JumpScreen, SymbolScreen, BookmarkScreen, HelpScreen { align: center middle; background: #000000 65%; }
    #jump-dialog {
        width: 54; height: auto; padding: 1 2; border: round #3b4261;
        background: #16161e;
    }
    #jump-input { margin: 1 0; }
    #jump-message { height: auto; }
    #symbol-dialog {
        width: 80%; max-width: 100; height: 75%; padding: 1 2;
        border: round #3b4261; background: #16161e;
    }
    #symbol-filter { height: 1; }
    #symbol-options { height: 1fr; margin: 1 0; }
    #help-dialog { width: 80; max-width: 95%; height: auto; max-height: 90%; padding: 1 3; border: round #3b4261; background: #16161e; }
    Screen.compact #timeline { text-wrap: nowrap; text-overflow: ellipsis; }
    """
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("ctrl+w", "window_prefix", "Change pane", show=False, priority=True),
        Binding("ctrl+n,down", "prompt_next", "Next result", show=False, priority=True),
        Binding("up", "prompt_previous", "Previous result", show=False, priority=True),
        Binding("ctrl+o", "find_file", "Files", priority=True),
        Binding("ctrl+f", "find_code", "Find code", priority=True),
        Binding("f3", "next_match", "Next match", show=False, priority=True),
        Binding("shift+f3", "previous_match", "Previous match", show=False, priority=True),
        Binding("escape", "close_find", "Close find", show=False),
        Binding("ctrl+l", "find_history", "Filter", priority=True),
        Binding("ctrl+d", "details", "Details", priority=True),
        Binding("f4", "cycle_source", "Source view", show=False, priority=True),
        Binding("f6", "nextchange", "Next change block", show=False, priority=True),
        Binding("shift+f6", "prevchange", "Previous change block", show=False, priority=True),
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
        Binding("alt+left", "back", "Previous visit", priority=True),
        Binding("alt+right", "forward", "Next visit", priority=True),
    ]

    def __init__(self, repository: Repository, initial: InitialSelection | None = None, *, store: StateStore | None = None):
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
        self.sidebar_width = 24
        self._sidebar_visible = True
        self._search_matches = ()
        self._match_index = -1
        self._rendering_timeline = False
        self._prompt_origin = None
        self._normal_prefix = ""
        self._symbol_cancel = threading.Event()
        self.store = store
        self.preferences = dict(DEFAULTS, keybindings={})
        self.init_navigation()
        self._valid_history_filter = ""
        self.diff_layout = "unified"
        self.diff_effective_layout = "unified"
        self._diff_key = None
        self._diff_patch = DiffPatch.parse("")
        self._diff_rows: tuple[SplitRow, ...] = ()
        self._diff_cursor: int | None = None
        self._diff_active = False
        self._diff_separator = 0
        self._diff_side: str | None = None
        self._diff_request = None
        self._diff_source_selection = None

    def compose(self) -> ComposeResult:
        yield Static(f"Git Trail  /  {self.repository.root.name}  /  {self.repository.commit[:7]}", id="workspace-title")
        with Horizontal(id="body"):
            with Vertical(id="files-pane"):
                yield Static("FILES", id="files-title")
                yield OptionList(id="files", markup=False)
            yield PaneDivider(id="sidebar-divider", tooltip="Drag to resize. Left/Right when focused.")
            with Vertical(id="workspace"):
                with Vertical(id="source-pane"):
                    with Horizontal(id="source-heading"):
                        yield Static("SOURCE · choose a file", id="source-title")
                        with Horizontal(id="source-toolbar"):
                            yield Button("After", id="view-after", classes="active", compact=True)
                            yield Button("Before", id="view-before", compact=True)
                            yield Button("Start", id="view-starting", compact=True, tooltip="Starting source for this timeline")
                    yield Static("Committed source · Ctrl+F to find", id="source-note")
                    yield SourceView(
                        read_only=True, show_line_numbers=True, soft_wrap=False,
                        id="source", tab_behavior="focus", theme="dracula",
                    )
                with Horizontal(id="history-pane"):
                    with Vertical(id="timeline-pane"):
                        yield Static("TIMELINE", id="timeline-title")
                        yield OptionList(id="timeline", markup=False)
                    with Vertical(id="diff-pane"):
                        with TabbedContent(id="review-tabs"):
                            with TabPane("Diff", id="diff-tab"):
                                with Horizontal(id="diff-heading"):
                                    yield Static("SELECTED-RANGE DIFF", id="diff-title")
                                    yield Button("Unified", id="diff-layout", compact=True, tooltip="Toggle unified or side-by-side diff (:split / :unified)")
                                with DiffScroll(id="diff-scroll"):
                                    yield DiffContent(
                                        "Choose a file, select lines, and press Enter.\n\n"
                                        "Ctrl+B finds functions and classes.\n"
                                        "Ctrl+F searches the displayed source.\n"
                                        "F1 opens shortcuts and filter examples.",
                                        id="diff", markup=False,
                                    )
                            with TabPane("Commit", id="commit-tab"):
                                with VerticalScroll(id="commit-scroll"):
                                    yield Static("Trace a selection to explore commit context.", id="commit-details", markup=False)
        with Horizontal(id="prompt-row"):
            yield Static("", id="prompt-label")
            yield Input(placeholder="fuzzy path or ext:py", id="file-filter")
            yield Input(placeholder="author: / after: / text", id="history-filter")
            yield Input(placeholder="help · files · symbols · filter · set ignorecase", id="command-input")
            with Horizontal(id="find-bar"):
                yield Input(placeholder="find text or function", id="find-input")
                yield Checkbox("Aa", id="find-case", tooltip="Match case")
                yield Checkbox(".*", id="find-regex", tooltip="Regular expression")
                yield Button("↑", id="find-previous", tooltip="Previous match (Shift+F3)")
                yield Button("↓", id="find-next", tooltip="Next match (F3)")
                yield Static("", id="find-count")
        with Horizontal(id="statusline"):
            yield Static("NORMAL", id="mode")
            yield Static("Reading repository…", id="status", markup=False)
            yield Static("", id="position")

    async def on_mount(self) -> None:
        if self.store is None and not self.is_headless:
            self.store = StateStore.default()
        state_warning = ""
        if self.store is not None:
            self.navigation_data = self.store.data
            try:
                self.store.allow_repository(self.repository.root)
                self.preferences = self.store.data["preferences"]
            except TrailError as error:
                state_warning = str(error)
            state_warning = state_warning or self.store.warning
        self.sidebar_width = self.preferences["sidebar_width"]
        self._sidebar_visible = self.preferences["sidebar_visible"]
        self.query_one("#files-pane").display = self._sidebar_visible
        self.query_one("#sidebar-divider").display = self._sidebar_visible
        self.query_one("#find-case", Checkbox).value = self.preferences["find_case"]
        self.query_one("#find-regex", Checkbox).value = self.preferences["find_regex"]
        self.close_prompt()
        self.query_one("#diff-layout", Button).active_effect_duration = 0
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
        elif self.initial.resume and self.repository_key in self.navigation_data["sessions"]:
            try:
                visit = Visit.from_dict(self.navigation_data["sessions"][self.repository_key])
                await self.restore_visit(visit)
            except TrailError as error:
                self.status(str(error), error=True)
        else:
            self.status(
                "Choose a file. All source and selections use the starting commit, HEAD by default."
            )
            self.action_find_file()
        if state_warning:
            self.status(state_warning, error=True)
            self.notify(state_warning, severity="warning")

    def exit(self, result=None, return_code: int = 0, message=None) -> None:
        self._symbol_cancel.set()
        if self.store is not None:
            try:
                for selector, key in (("#find-case", "find_case"), ("#find-regex", "find_regex")):
                    fields = list(self.query(selector))
                    if fields:
                        self.preferences[key] = fields[0].value
                self.store.data["preferences"] = self.preferences
                visit = self.current_visit()
                if visit is not None:
                    self.navigation_data["sessions"][self.repository_key] = visit.to_dict()
                self.store.save()
            except TrailError as error:
                message = Text(str(error), style="#f7768e")
        super().exit(result, return_code, message)

    def status(self, message: str, error: bool = False) -> None:
        self.query_one("#status", Static).update(Text(message, style="#f49b9b" if error else "#9cafbf"))

    def set_editor_mode(self, mode: str) -> None:
        if self.query_one("#prompt-row").display:
            mode = "PROMPT"
        indicator = self.query_one("#mode", Static)
        indicator.update(mode)
        indicator.set_class(mode.startswith("V"), "visual")
        indicator.set_class(mode == "PROMPT", "prompt")

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        self._normal_prefix = ""
        if not isinstance(event.widget, SourceView) and not isinstance(self.screen, ModalScreen):
            self.set_editor_mode("NORMAL")

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in ("nextchange", "prevchange"):
            return True
        if action == "diffsource":
            return self.diff_navigation_allowed()
        if action in ("window_prefix", "back", "forward"):
            return not isinstance(self.focused, Input) and not isinstance(self.screen, ModalScreen)
        if action in ("prompt_next", "prompt_previous"):
            return isinstance(self.focused, Input) and self.focused.id in ("file-filter", "history-filter") and not isinstance(self.screen, ModalScreen)
        if action == "paragraph":
            return not isinstance(self.screen, ModalScreen) and (
                not isinstance(self.focused, Input) or self.focused.id in ("file-filter", "history-filter")
            )
        return True

    def action_window_prefix(self) -> None:
        source = self.query_one("#source", SourceView)
        source.vim.leave_visual(source, collapse=False)
        self._normal_prefix = "window"
        self.status("Ctrl+W  h/j/k/l: move to pane · w: next pane")

    def focus_window(self, key: str) -> None:
        review = "#commit-scroll" if self.query_one("#review-tabs", TabbedContent).active == "commit-tab" else "#diff-scroll"
        panes = ["#files", "#source", "#timeline", review]
        current = next((selector for selector in panes if self.query_one(selector) is self.focused), "#source")
        if key == "w":
            visible = [selector for selector in panes if self.query_one(selector).is_on_screen]
            target = visible[(visible.index(current) + 1) % len(visible)] if current in visible else "#source"
        else:
            target = {
                "#files": {"l": "#source"},
                "#source": {"h": "#files", "j": "#timeline"},
                "#timeline": {"h": "#files", "k": "#source", "l": review},
                review: {"h": "#timeline", "k": "#source"},
            }.get(current, {}).get(key, current)
        if target == "#files" and not self._sidebar_visible:
            self.action_toggle_sidebar()
        self.query_one(target).focus()
        self.status("NORMAL · / search · v/V select · Enter trace · Ctrl+W panes")

    def prompt_result(self, delta: int) -> None:
        selector = "#files" if self.focused and self.focused.id == "file-filter" else "#timeline"
        options = self.query_one(selector, OptionList)
        if options.option_count:
            options.highlighted = max(0, min((options.highlighted or 0) + delta, options.option_count - 1))
            options.scroll_to_highlight()

    def action_prompt_next(self) -> None:
        self.prompt_result(1)

    def action_prompt_previous(self) -> None:
        self.prompt_result(-1)

    def on_key(self, event: events.Key) -> None:
        if not isinstance(self.focused, SourceView):
            self.handle_normal_key(event)

    def handle_normal_key(self, event: events.Key) -> bool:
        if isinstance(self.focused, Input) or isinstance(self.screen, ModalScreen):
            return False
        key = event.character or event.key
        handled = True
        normal_source = not isinstance(self.focused, SourceView) or self.focused.vim.mode == "NORMAL"
        if not self._normal_prefix and normal_source and key in self.preferences["keybindings"]:
            getattr(self, "action_" + NORMAL_ACTIONS[self.preferences["keybindings"][key]])()
        elif self.handle_diff_key(event):
            return True
        elif self._normal_prefix == "window":
            self._normal_prefix = ""
            self.focus_window(key)
        elif self._normal_prefix in ("leader", "leader-find", "leader-history"):
            prefix = self._normal_prefix
            self._normal_prefix = ""
            if prefix == "leader" and key == "f":
                self._normal_prefix = "leader-find"
                self.status("Space f  f: files · s: symbols · h: history")
            elif prefix == "leader" and key == "h":
                self._normal_prefix = "leader-history"
                self.status("Space h  b: back · f: forward · m: bookmarks")
            elif prefix == "leader-history" and key in ("b", "f", "m"):
                {"b": self.action_back, "f": self.action_forward, "m": self.action_bookmarks}[key]()
            elif prefix == "leader-find" and key in ("f", "s", "h"):
                {"f": self.action_find_file, "s": self.action_browse_symbols, "h": self.action_find_history}[key]()
            else:
                self.status("Sequence cancelled · ? help")
        elif key == " ":
            source = self.query_one("#source", SourceView)
            source.vim.leave_visual(source, collapse=False)
            self._normal_prefix = "leader"
            self.status("Space  f: find · h: visits and bookmarks · Escape cancels")
        elif key == ":":
            self.action_command()
        elif key == "/":
            if self.focused is self.query_one("#files"):
                self.action_find_file()
            elif self.focused is self.query_one("#timeline"):
                self.action_find_history()
            else:
                self.action_find_code()
        elif key in ("n", "N"):
            self.action_next_match() if key == "n" else self.action_previous_match()
        elif key == "?":
            self.action_help()
        elif key == "q":
            self.exit()
        elif isinstance(self.focused, OptionList) and key in ("j", "k", "g", "G"):
            options = self.focused
            if key == "g" and self._normal_prefix != "g":
                self._normal_prefix = "g"
            else:
                if options.option_count:
                    index = options.highlighted or 0
                    options.highlighted = {
                        "j": min(index + 1, options.option_count - 1), "k": max(0, index - 1),
                        "g": 0, "G": options.option_count - 1,
                    }[key]
                    options.scroll_to_highlight()
                self._normal_prefix = ""
        elif isinstance(self.focused, VerticalScroll) and key in ("j", "k", "g", "G", "h", "l"):
            pane = self.focused
            if key == "g" and self._normal_prefix != "g":
                self._normal_prefix = "g"
            else:
                if key in ("j", "k"):
                    pane.scroll_relative(y=1 if key == "j" else -1, animate=False)
                elif key in ("h", "l"):
                    pane.scroll_relative(x=1 if key == "l" else -1, animate=False)
                else:
                    pane.scroll_to(y=0 if key == "g" else pane.max_scroll_y, animate=False)
                self._normal_prefix = ""
        else:
            self._normal_prefix = ""
            handled = False
        if handled:
            event.prevent_default()
            event.stop()
        return handled

    def open_prompt(self, target: str) -> None:
        if not self.query_one("#prompt-row").display:
            self._prompt_origin = self.focused
        self._normal_prefix = ""
        self.query_one("#source", SourceView).vim.leave_visual(self.query_one("#source", SourceView), collapse=False)
        self.query_one("#prompt-row").display = True
        for selector in ("#file-filter", "#history-filter", "#find-bar", "#command-input"):
            self.query_one(selector).display = selector == target
        self.set_editor_mode("PROMPT")
        self.query_one("#prompt-label", Static).update({
            "#file-filter": "files > ", "#history-filter": "filter > ", "#find-bar": "/ ", "#command-input": ": ",
        }[target])
        field = "#find-input" if target == "#find-bar" else target
        self.query_one(field, Input).focus()

    def close_prompt(self) -> None:
        self.query_one("#prompt-row").display = False
        for selector in ("#file-filter", "#history-filter", "#find-bar", "#command-input"):
            self.query_one(selector).display = False
        self.set_editor_mode("NORMAL")

    @on(TextArea.SelectionChanged, "#source")
    def source_selection_changed(self, event: TextArea.SelectionChanged) -> None:
        source = self.query_one("#source", SourceView)
        if self._diff_request and source.selection != self._diff_source_selection:
            self.cancel_diff_jump()
        if source.selection != source.vim.expected:
            source.vim.sync(source)
            self.set_editor_mode("NORMAL")
        row, column = source.vim.cursor if source.vim.mode != "NORMAL" else source.selection.end
        self.query_one("#position", Static).update(f"{row + 1}:{column + 1}")

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
        self.record_visit()
        self.begin_navigation()
        self._symbol_cancel.set()
        self.close_prompt()
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
        message = "NORMAL · v/V selects · Enter traces · / finds · Ctrl+W panes · ? help"
        if snapshot.working_tree_changed:
            message = "Local edits exist. Showing committed source. " + message
        self.status(message)
        if selection:
            self.select_range(selection)
            await self.trace_selection(selection, follow=self.initial.follow_symbol)
        elif self.initial.follow_symbol:
            self.action_follow()
        else:
            self._navigation_ready = True
            self.record_visit()

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
        source = self.query_one("#source", SourceView)
        source.vim.leave_visual(source, collapse=False)
        self.run_worker(
            self.trace_selection(selection), group="history", exclusive=True, exit_on_error=False
        )

    async def trace_selection(self, selection: LineRange, *, follow: bool = False) -> None:
        if self.snapshot is None:
            return
        self.record_visit()
        self.begin_navigation()
        snapshot = self.snapshot
        repository = self.snapshot_repository
        self._symbol_cancel.set()
        self._symbol_cancel = threading.Event()
        cancellation = self._symbol_cancel
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
            if follow:
                timeline = await asyncio.to_thread(repository.symbol_history, snapshot, selection, cancel=cancellation)
            else:
                timeline = await asyncio.to_thread(repository.history, snapshot, selection)
        except asyncio.CancelledError:
            cancellation.set()
            raise
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
        if timeline.mode == "symbol":
            note = "\n".join(timeline.warnings)
            self.query_one("#source-note", Static).update(Text(note))
            self.query_one("#source-note", Static).add_class("important")
            self.status(f"Symbol history · {len(timeline.entries)} changes · " + timeline.warnings[-1],
                        error=any("Partial" in warning for warning in timeline.warnings))
            if not timeline.entries:
                self.snapshot = snapshot
                self.snapshot_repository = repository
                self.display_snapshot(snapshot)
                self.select_range(selection)
                self.query_one("#source-note", Static).update(Text(note))
                self.query_one("#source-note", Static).add_class("important")
                self.query_one("#diff", Static).update(Text(note))
                self.status(timeline.warnings[-1], error=True)
                self._navigation_ready = True
                self.record_visit()

    def on_unmount(self) -> None:
        self._symbol_cancel.set()

    def apply_history_filter(self, *, load_view: bool = True) -> None:
        if self.timeline is None:
            return
        try:
            predicate = HistoryFilter.parse(self.query_one("#history-filter", Input).value)
        except TrailError as error:
            self.status(str(error), error=True)
            return
        if load_view:
            self.record_visit()
            self._navigation_ready = False
        self._valid_history_filter = self.query_one("#history-filter", Input).value
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
            if load_view:
                self.show_entry(index)
            else:
                self.selected_entry = self.filtered_entries[index]
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
            self._navigation_ready = True
            self.record_visit()

    @on(Input.Changed, "#history-filter")
    def history_filter_changed(self) -> None:
        self.cancel_diff_jump()
        if self._restoring:
            ready = self._restore_previous_ready
            self.begin_navigation()
            self._navigation_ready = ready
        self.apply_history_filter()

    @on(Input.Submitted, "#history-filter")
    def history_filter_submitted(self) -> None:
        self.close_prompt()
        self.query_one("#timeline", OptionList).focus()

    @on(OptionList.OptionHighlighted, "#timeline")
    def entry_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if self.selected_entry is not None and self.selected_entry.commit == event.option.id:
            return
        if not self._rendering_timeline and event.option.id in {entry.commit for entry in self.filtered_entries}:
            index = next(index for index, entry in enumerate(self.filtered_entries) if entry.commit == event.option.id)
            self.show_entry(index)

    def show_entry(self, index: int) -> None:
        if self.timeline is None or not 0 <= index < len(self.filtered_entries):
            return
        self.record_visit()
        self.begin_navigation()
        entry = self.filtered_entries[index]
        self.selected_entry = entry
        self.query_one("#diff-title", Static).update(
            Text(f"{entry.commit[:10]} · {entry.author} · committed {entry.committed_at}")
        )
        self.render_diff()
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

    async def load_entry(self, entry: HistoryEntry, generation: int, mode: str, *, prepared=None) -> None:
        try:
            if prepared is not None:
                snapshot, selection, note, details, repository = prepared
            else:
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
        if self.timeline and self.timeline.mode == "symbol":
            partial = [warning for warning in self.timeline.warnings if "Partial" in warning]
            note = " · ".join([*partial, entry.tracking_note])
            self.query_one("#source-note", Static).update(Text(note or "Symbol history uses first parents."))
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
        if self.timeline and self.timeline.mode == "symbol":
            context.append("\nSYMBOL TRACKING\n" + entry.tracking_note + "\n\n" + "\n".join(self.timeline.warnings))
        self.query_one("#commit-details", Static).update(context)
        self.status(
            f"{len(self.timeline.entries)} changes · {mode.capitalize()} {entry.commit[:8]} · Ctrl+J/K navigate · F1 help"
            + (" · local edits excluded" if self.traced_snapshot and self.traced_snapshot.working_tree_changed else "")
            + (" · shallow history" if self.timeline and any("shallow" in warning for warning in self.timeline.warnings) else "")
        )
        if self.timeline and self.timeline.mode == "symbol" and partial:
            self.status(partial[-1], error=True)
        self._navigation_ready = True
        self.record_visit()

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
        self.open_prompt("#file-filter")

    def display_snapshot(self, snapshot: Snapshot) -> None:
        source = self.query_one("#source", SourceView)
        language = {
            ".py": "python", ".js": "javascript", ".go": "go", ".rs": "rust",
            ".java": "java", ".sh": "bash", ".json": "json", ".html": "html",
            ".css": "css", ".md": "markdown", ".toml": "toml", ".yaml": "yaml", ".yml": "yaml",
        }.get(PurePosixPath(snapshot.path).suffix.lower())
        source.configure_language(language)
        source.load_text(display_source(snapshot.text))
        self.query_one("#source-title", Static).update(Text(f"{snapshot.path} · {snapshot.commit[:7]}"))
        self.query_one("#source-note", Static).update("Committed source · Ctrl+F to find")
        self.query_one("#source-note", Static).remove_class("important")
        self.refresh_search()

    def position_diff(self) -> None:
        if self.selected_entry is None:
            return
        self.refresh_diff_layout()
        if not self._diff_active:
            self.query_one("#diff-scroll", VerticalScroll).scroll_to(y=0, animate=False, immediate=True)

    def render_diff(self) -> None:
        if self.selected_entry is None:
            return
        key = self.selected_entry.commit, self.selected_entry.patch
        if key != self._diff_key:
            self._diff_key = key
            self._diff_patch = DiffPatch.parse(self.selected_entry.patch)
            targets = self._diff_patch.change_targets()
            self._diff_cursor = targets[0] if targets else None
            self._diff_active = False
            self._diff_side = None
        pane = self.query_one("#diff-scroll", VerticalScroll)
        width = pane.scrollable_content_region.width
        old_layout = self.diff_effective_layout
        self.diff_effective_layout = "split" if self.diff_layout == "split" and width >= 60 else "unified"
        button = self.query_one("#diff-layout", Button)
        button.label = "Split" if self.diff_effective_layout == "split" else "Unified"
        button.tooltip = "Side-by-side needs 60 columns in the diff pane. Unified fallback is active." if self.diff_layout != self.diff_effective_layout else "Toggle unified or side-by-side diff (:split / :unified)"
        if old_layout != self.diff_effective_layout and self.diff_layout == "split":
            self.status("Side-by-side diff" if self.diff_effective_layout == "split" else "Unified diff (side-by-side needs 60 diff columns)")
        active = self._diff_cursor if self._diff_active else None
        if self.diff_effective_layout == "split":
            rendered, rows, self._diff_separator = self._diff_patch.side_by_side(
                self.selected_entry.old_path, self.selected_entry.new_path, width, active,
                compact=self.screen.has_class("compact"),
            )
            self._diff_rows = (SplitRow(), *rows)
            self.query_one("#diff", Static).update(rendered)
            return
        patch = display_source(self.selected_entry.patch)
        offset = 0
        if self.screen.has_class("compact"):
            if self._diff_patch.hunks:
                offset = self._diff_patch.hunks[0].header
                patch = "\n".join(patch.split("\n")[offset:])
        self._diff_rows = tuple(SplitRow(heading=index) for index in range(offset, len(self._diff_patch.lines)))
        syntax = Syntax(patch, "diff", theme=DIFF_THEME, word_wrap=False, background_color=BACKGROUND)
        if active is not None and active >= offset:
            syntax.stylize_range("on #283457", (active - offset + 1, 0), (active - offset + 2, 0))
        self.query_one("#diff", Static).update(syntax)

    def set_diff_layout(self, layout: str) -> None:
        self.diff_layout = layout
        self.query_one("#review-tabs", TabbedContent).active = "diff-tab"
        self.refresh_diff_layout()
        self.query_one("#diff-scroll", VerticalScroll).focus()
        self.status("Side-by-side diff" if self.diff_effective_layout == "split" else
                    ("Unified diff (side-by-side needs 60 diff columns)" if layout == "split" else "Unified diff"))

    @on(Button.Pressed, "#diff-layout")
    def diff_layout_pressed(self) -> None:
        self.set_diff_layout("unified" if self.diff_layout == "split" else "split")

    @on(Button.Pressed, "#view-after, #view-before, #view-starting")
    def source_view_pressed(self, event: Button.Pressed) -> None:
        self.set_source_mode(event.button.id.removeprefix("view-"))

    def set_source_mode(self, mode: str) -> None:
        if self.selected_entry is None and mode != "after":
            self.status("Trace a selection before choosing a historical source view.")
            return
        self.record_visit()
        if self.selected_entry is not None:
            self._navigation_ready = False
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
        self.open_prompt("#history-filter")

    def action_command(self) -> None:
        self.open_prompt("#command-input")
        self.query_one("#command-input", Input).value = ""

    @on(Input.Submitted, "#command-input")
    def command_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip().removeprefix(":").strip()
        name, _, argument = value.partition(" ")
        name, argument = name.lower(), argument.strip()
        if name in ("bookmark", "open", "unbookmark"):
            try:
                {"bookmark": self.save_bookmark, "open": self.open_bookmark, "unbookmark": self.delete_bookmark}[name](argument)
            except TrailError as error:
                self.status(str(error), error=True)
                return
            self.close_prompt()
            self.query_one("#source").focus()
            return
        if name in ("map", "unmap"):
            try:
                parts = argument.split()
                if name == "map" and len(parts) == 2:
                    validate_mapping(*parts)
                    if parts[0] not in self.preferences["keybindings"] and len(self.preferences["keybindings"]) >= 50:
                        raise TrailError("Save at most 50 normal-mode mappings.")
                    self.preferences["keybindings"][parts[0]] = parts[1]
                elif name == "unmap" and len(parts) == 1:
                    validate_mapping(parts[0], "help")
                    self.preferences["keybindings"].pop(parts[0], None)
                else:
                    raise TrailError("Use :map KEY ACTION or :unmap KEY. :maps lists mappings.")
            except TrailError as error:
                self.status(str(error), error=True)
                return
            self.action_close_find()
            self.status(f"Normal-mode mapping {name} updated. Saved on exit.")
            return
        settings = {"ignorecase": ("#find-case", False), "noignorecase": ("#find-case", True),
                    "regex": ("#find-regex", True), "noregex": ("#find-regex", False)}
        actions = {
            "help": self.action_help, "symbols": self.action_browse_symbols,
            "maps": self.action_help,
            "follow": self.action_follow,
            "back": self.action_back, "forward": self.action_forward,
            "bookmarks": self.action_bookmarks,
            "sidebar": self.action_toggle_sidebar,
            "before": lambda: self.set_source_mode("before"),
            "after": lambda: self.set_source_mode("after"),
            "starting": lambda: self.set_source_mode("starting"),
            "diff": lambda: setattr(self.query_one("#review-tabs", TabbedContent), "active", "diff-tab"),
            "split": lambda: self.set_diff_layout("split"),
            "unified": lambda: self.set_diff_layout("unified"),
            "nextchange": self.action_nextchange,
            "prevchange": self.action_prevchange,
            "diffsource": self.action_diffsource,
            "diffbefore": lambda: self.jump_diff_source("before"),
            "diffafter": lambda: self.jump_diff_source("after"),
            "commit": lambda: setattr(self.query_one("#review-tabs", TabbedContent), "active", "commit-tab"),
        }
        if name == "set" and argument not in settings:
            self.status("Settings: ignorecase, noignorecase, regex, noregex", error=True)
            return
        if name == "filter":
            try:
                HistoryFilter.parse(argument)
            except TrailError as error:
                self.status(str(error), error=True)
                return
        numeric = name.isascii() and name.isdecimal() and not argument
        if numeric and len(name) > 9:
            self.status("Line number is too large.", error=True)
            return
        line = int(name) if numeric else None
        if line is not None and (not self.snapshot or not 1 <= line <= len(self.snapshot.lines)):
            self.status(f"Enter a line from 1 to {len(self.snapshot.lines) if self.snapshot else 0}.", error=True)
            return
        if line is None and name not in (*actions, "q", "quit", "set", "file", "files", "filter"):
            self.status(f"Unknown command: {value or '(empty)'}. Use :help.", error=True)
            return
        if argument and name not in ("file", "files", "filter", "set"):
            self.status(f":{name} takes no argument.", error=True)
            return
        self.close_prompt()
        self.query_one("#source" if self.snapshot else "#files").focus()
        if line is not None:
            self.jump_to(line)
        elif name in ("q", "quit"):
            self.exit()
        elif name == "set":
            selector, enabled = settings[argument]
            self.query_one(selector, Checkbox).value = enabled
            self.status(f"Search: {argument}")
        elif name in ("file", "files"):
            self.action_find_file()
            if argument:
                self.query_one("#file-filter", Input).value = argument
        elif name == "filter":
            self.query_one("#history-filter", Input).value = argument
            self.query_one("#timeline", OptionList).focus()
        else:
            if name in ("nextchange", "prevchange", "diffsource", "diffbefore", "diffafter"):
                self.call_after_refresh(actions[name])
            else:
                actions[name]()

    def action_bookmarks(self) -> None:
        try:
            items = self.saved_bookmarks()
            if not items:
                raise TrailError("No bookmarks yet. Use :bookmark NAME.")
            screen = BookmarkScreen(items)
        except TrailError as error:
            self.status(str(error), error=True)
            return

        def selected(name: str | None) -> None:
            if name:
                try:
                    self.open_bookmark(name)
                except TrailError as error:
                    self.status(str(error), error=True)

        self.push_screen(screen, selected)

    def action_find_code(self) -> None:
        if self.snapshot is None:
            self.status("Choose a file or source revision first.", error=True)
            return
        self.open_prompt("#find-bar")
        field = self.query_one("#find-input", Input)
        source = self.query_one("#source", SourceView)
        selected = source.selected_text
        if selected and "\n" not in selected:
            field.value = selected
        field.focus()
        self.refresh_search()

    def action_close_find(self) -> None:
        self._normal_prefix = ""
        if self.query_one("#prompt-row").display:
            self.close_prompt()
            origin = self._prompt_origin
            if origin is not None and origin.display and origin.is_mounted:
                origin.focus()
            else:
                self.query_one("#source" if self.snapshot else "#files").focus()
        elif isinstance(self.focused, SourceView):
            self.focused.vim.leave_visual(self.focused)

    @on(Input.Changed, "#find-input")
    @on(Checkbox.Changed, "#find-case, #find-regex")
    def find_changed(self) -> None:
        self.preferences["find_case"] = self.query_one("#find-case", Checkbox).value
        self.preferences["find_regex"] = self.query_one("#find-regex", Checkbox).value
        self.refresh_search()

    @on(Input.Submitted, "#find-input")
    def find_submitted(self) -> None:
        self.close_prompt()
        self.query_one("#source", SourceView).focus()

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
        if not field.value:
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
        if self._search_matches and self.query_one("#find-bar").display:
            self.show_match(0)
        elif self._search_matches:
            self.query_one("#find-count", Static).update(f"{len(self._search_matches)} matches")
        else:
            self.query_one("#find-count", Static).update("0 matches")

    def show_match(self, index: int) -> None:
        if not self._search_matches:
            return
        self._match_index = index % len(self._search_matches)
        start, end = self._search_matches[self._match_index]
        source = self.query_one("#source", SourceView)
        source.vim.reset()
        source.move_cursor(start, center=True)
        source.move_cursor(end, select=True)
        self.query_one("#find-count", Static).update(f"{self._match_index + 1}/{len(self._search_matches)}")
        if not self.query_one("#find-bar").display:
            self.status(f"/{self.query_one('#find-input', Input).value} · {self._match_index + 1}/{len(self._search_matches)}")

    def action_next_match(self) -> None:
        self.show_match(self._match_index + 1)

    def action_previous_match(self) -> None:
        self.show_match(self._match_index - 1 if self._match_index >= 0 else -1)

    def resize_sidebar(self, width: int, *, available: int | None = None) -> None:
        if available is None:
            available = self.query_one("#body").size.width or self.size.width
        maximum = max(16, min(70, available - 55))
        self.sidebar_width = max(16, min(width, maximum))
        self.preferences["sidebar_width"] = self.sidebar_width
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
        self.preferences["sidebar_visible"] = self._sidebar_visible
        self.query_one("#files-pane").display = self._sidebar_visible
        self.query_one("#sidebar-divider").display = self._sidebar_visible
        self.query_one("#source", SourceView).focus()

    def action_help(self) -> None:
        self.close_prompt()
        self.query_one("#source" if self.snapshot else "#files").focus()
        self.push_screen(HelpScreen(self.preferences["keybindings"]))

    def action_jump(self) -> None:
        if self.snapshot is None:
            self.status("Choose a file first.", error=True)
            return
        self.close_prompt()
        self.query_one("#source", SourceView).focus()
        self.push_screen(JumpScreen(len(self.snapshot.lines)), self.jump_to)

    def jump_to(self, line: int | None) -> None:
        if line is not None and self.snapshot:
            self.query_one("#source", SourceView).move_cursor((line - 1, 0), center=True)
            self.query_one("#source", SourceView).focus()

    def action_paragraph(self) -> None:
        if isinstance(self.focused, Input) and self.focused.id in ("file-filter", "history-filter"):
            self.action_prompt_previous()
            return
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

    def action_follow(self) -> None:
        if self.snapshot is None:
            self.status("Choose a source revision first.", error=True)
            return
        try:
            chosen = self.selected_range()
            items = symbols(self.snapshot.text, self.snapshot.path)
            if not any(item.start == chosen.start and item.end == chosen.end for item in items):
                source = self.query_one("#source", SourceView)
                line = source.vim.cursor[0] + 1 if source.vim.mode != "NORMAL" else source.cursor_location[0] + 1
                chosen = enclosing_symbol(self.snapshot.text, self.snapshot.path, line)
            self.select_range(chosen)
        except TrailError as error:
            self.status(str(error), error=True)
            return
        self.run_worker(self.trace_selection(chosen, follow=True), group="history", exclusive=True, exit_on_error=False)

    def action_browse_symbols(self) -> None:
        if self.snapshot is None:
            self.status("Choose a file first.", error=True)
            return
        self.close_prompt()
        self.query_one("#source", SourceView).focus()
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

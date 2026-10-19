from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from .git import CommitDetails, HistoryView, TrailError, display_source
from .merge import CommitMetadata, MergeList, ParentComparison
from .navigation import Visit


@dataclass(frozen=True)
class ParentChoice:
    index: int
    commit: str
    subject: str
    available: bool
    note: str = ""


class _ChoiceScreen(ModalScreen[str | None]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+n,down", "next_choice", "Next", show=False, priority=True),
        Binding("ctrl+p,up", "previous_choice", "Previous", show=False, priority=True),
    ]
    DEFAULT_CSS = """
    _ChoiceScreen { align: center middle; background: $trail-overlay 65%; }
    .merge-dialog {
        width: 90%; max-width: 100; height: 75%; padding: 1 2;
        border: round $trail-border; background: $trail-sidebar;
    }
    .merge-choices { height: 1fr; margin: 1 0; border: none; background: $trail-sidebar; }
    .merge-hint { height: auto; color: $trail-muted; }
    """
    filter_id = "merge-filter"
    options_id = "merge-options"

    def __init__(self, title: str, choices: tuple[tuple[str, str], ...], *, current: str = "", note: str = ""):
        super().__init__()
        self.title_text = title
        self.choices = choices
        self.filtered_choices = choices
        self.current = current
        self.note = note

    def compose(self) -> ComposeResult:
        with Vertical(classes="merge-dialog"):
            yield Static(Text(self.title_text), classes="dialog-title")
            yield Input(placeholder="Filter by SHA, subject, or parent number", id=self.filter_id)
            yield OptionList(id=self.options_id, classes="merge-choices", markup=False)
            yield Static(Text(self.note + "\nCtrl+N/P or Up/Down · Enter / click selects · Escape cancels"),
                         classes="merge-hint")

    def on_mount(self) -> None:
        self.filter_choices("")
        options = self.query_one(OptionList)
        if self.current:
            options.highlighted = next((index for index, item in enumerate(self.filtered_choices)
                                        if item[0] == self.current), options.highlighted)
        self.query_one(Input).focus()

    def filter_choices(self, query: str) -> None:
        words = query.casefold().split()
        self.filtered_choices = tuple(
            item for item in self.choices if all(word in item[1].casefold() for word in words)
        )
        options = self.query_one(OptionList)
        options.set_options(Option(Text(label), id=key) for key, label in self.filtered_choices)
        options.highlighted = 0 if self.filtered_choices else None

    @on(Input.Changed)
    def filter_changed(self, event: Input.Changed) -> None:
        self.filter_choices(event.value)

    @on(Input.Submitted)
    def submitted(self) -> None:
        options = self.query_one(OptionList)
        if options.highlighted is not None:
            self.dismiss(options.get_option_at_index(options.highlighted).id)

    @on(OptionList.OptionSelected)
    def selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id in {item[0] for item in self.filtered_choices}:
            self.dismiss(event.option.id)

    def move_choice(self, delta: int) -> None:
        options = self.query_one(OptionList)
        if options.option_count:
            options.highlighted = max(0, min((options.highlighted or 0) + delta, options.option_count - 1))
            options.scroll_to_highlight()

    def action_next_choice(self) -> None:
        self.move_choice(1)

    def action_previous_choice(self) -> None:
        self.move_choice(-1)

    def on_key(self, event: events.Key) -> None:
        if isinstance(self.focused, OptionList) and event.character in ("j", "k"):
            self.move_choice(1 if event.character == "j" else -1)
            event.prevent_default()
            event.stop()

    def action_cancel(self) -> None:
        self.dismiss(None)


class MergePickerScreen(_ChoiceScreen):
    def __init__(self, result: MergeList):
        self.result = result
        choices = tuple(
            (item.commit, f"{item.commit} · {item.committed_at[:10]} · {len(item.parents)} parents\n"
                          + display_source(item.subject))
            for item in result.merges
        )
        note = "Discovery is limited; older merges or shallow boundaries may be omitted." if result.truncated else ""
        super().__init__("REACHABLE MERGES", choices, note=note)


class ParentPickerScreen(_ChoiceScreen):
    filter_id = "parent-filter"
    options_id = "parent-options"

    def __init__(self, metadata: CommitMetadata, parents: tuple[ParentChoice, ...], *, current: str = ""):
        self.metadata = metadata
        self.parents = parents
        choices = tuple(
            (item.commit, f"Parent {item.index}/{len(metadata.parents)} · {item.commit}\n"
                          + display_source(item.subject if item.available else "Unavailable object · " + item.note))
            for item in parents
        )
        super().__init__("MERGE PARENTS · " + metadata.commit, choices, current=current,
                         note="Compare the full committed file with this parent; this includes merge resolutions.")


@dataclass(frozen=True)
class PreparedComparison:
    comparison: ParentComparison
    view: HistoryView
    details: CommitDetails
    repository: object
    mode: str


class MergeNavigationSupport:
    def init_merges(self) -> None:
        self.comparison: ParentComparison | None = None
        self._comparison_entry = None
        self._comparison_base_visit: Visit | None = None
        self._merge_generation = 0
        self._merge_pending_context = None
        self._timeline_repository = self.repository

    @property
    def review_entry(self):
        return self._comparison_entry if self.comparison is not None else self.selected_entry

    def cancel_merge_requests(self) -> None:
        self._merge_generation += 1
        self._merge_pending_context = None
        for group in ("merge-picker", "merge-review"):
            self.workers.cancel_group(self, group)

    def leave_comparison(self) -> None:
        self.cancel_merge_requests()
        self.comparison = None
        self._comparison_entry = None
        self._comparison_base_visit = None
        self.refresh_parent_control()

    def refresh_parent_control(self) -> None:
        buttons = list(self.query("#merge-parent"))
        if not buttons:
            return
        button = buttons[0]
        if self.comparison:
            button.label = f"P{self.comparison.parent_index}/{len(self.comparison.parents)}"
            button.tooltip = self.comparison.note + "\n:parents chooses another parent; :merges lists historical merges."
        else:
            button.label = "Parents"
            button.tooltip = "Choose merge parent (:parents). :merges lists reachable historical merges."

    def _merge_context(self):
        context = (self._merge_generation, self._navigation_generation, self._view_generation,
                   self.comparison, self.selected_entry, self.repository,
                   self.query_one("#source").selection, self.source_mode)
        self._merge_pending_context = context
        return context

    def _merge_context_matches(self, context) -> bool:
        return (
            context[:3] == (self._merge_generation, self._navigation_generation, self._view_generation)
            and context[3] is self.comparison and context[4] is self.selected_entry
            and context[5] is self.repository
            and context[6] == self.query_one("#source").selection and context[7] == self.source_mode
        )

    def merge_path(self) -> str:
        if self.comparison:
            return self.comparison.path
        anchor = self.snapshot or self.traced_snapshot
        if anchor is None:
            raise TrailError("Choose a committed file before reviewing merge parents.")
        return anchor.path

    def _merge_allowed(self) -> bool:
        if isinstance(self.screen, ModalScreen) or isinstance(self.focused, Input):
            return False
        self.cancel_diff_jump()
        return self._navigation_ready

    def close_merge_prompt(self) -> None:
        origin = self._prompt_origin
        self.close_prompt()
        if origin is not None and origin.is_on_screen and not isinstance(origin, Input):
            origin.focus()
        else:
            self.query_one("#source" if self.snapshot is not None else "#files").focus()

    def parent_targets(self, commit: str | None, path: str) -> tuple[tuple[str, str], ...]:
        if commit:
            return ((commit, path),)
        if self.comparison:
            return ((self.comparison.commit, self.comparison.path),)
        targets = []
        if self.selected_entry:
            targets.append((self.selected_entry.commit,
                            self.selected_entry.new_path or self.selected_entry.old_path or path))
        anchor = self.traced_snapshot or self.snapshot
        if anchor is not None:
            targets.append((anchor.commit, anchor.path))
        return tuple(dict.fromkeys(targets))

    def prepare_parent_choices(self, repository, targets: tuple[tuple[str, str], ...]):
        metadata = None
        path = ""
        for commit, candidate_path in targets:
            candidate = repository.commit_metadata(commit)
            if len(candidate.parents) > 1:
                metadata, path = candidate, candidate_path
                break
        if metadata is None:
            raise TrailError("This change and starting source are not merges. Use :merges to choose a historical merge.")
        parents = []
        for index, parent in enumerate(metadata.parents, 1):
            try:
                item = repository.commit_metadata(parent)
                parents.append(ParentChoice(index, parent, item.subject, True))
            except TrailError as error:
                parents.append(ParentChoice(index, parent, "", False, str(error)))
        return metadata, tuple(parents), path

    def action_merges(self) -> None:
        if not self._merge_allowed():
            return
        try:
            path = self.merge_path()
        except TrailError as error:
            self.status(str(error), error=True)
            return
        self.cancel_merge_requests()
        context = self._merge_context()
        repository = self.repository

        async def show() -> None:
            try:
                result = await asyncio.to_thread(repository.reachable_merges)
            except TrailError as error:
                if self._merge_context_matches(context):
                    self._merge_pending_context = None
                    self.status(str(error), error=True)
                return
            if not self._merge_context_matches(context) or isinstance(self.screen, ModalScreen):
                return
            if not result.merges:
                self._merge_pending_context = None
                self.status("No reachable merge commits at the selected starting ref.")
                return

            def selected(commit: str | None) -> None:
                if commit and self._merge_context_matches(context):
                    self.open_parent_picker(commit, path)
                elif commit is None and self._merge_context_matches(context):
                    self.cancel_merge_requests()

            self.push_screen(MergePickerScreen(result), selected)

        self.run_worker(show, group="merge-picker", exclusive=True, exit_on_error=False)

    def action_parents(self) -> None:
        self.open_parent_picker()

    def open_parent_picker(self, commit: str | None = None, path: str | None = None) -> None:
        if not self._merge_allowed():
            return
        try:
            path = path or self.merge_path()
        except TrailError as error:
            self.status(str(error), error=True)
            return
        self.cancel_merge_requests()
        context = self._merge_context()
        repository = self.repository
        targets = self.parent_targets(commit, path)

        async def show() -> None:
            try:
                metadata, parents, chosen_path = await asyncio.to_thread(
                    self.prepare_parent_choices, repository, targets,
                )
            except TrailError as error:
                if self._merge_context_matches(context):
                    self._merge_pending_context = None
                    self.status(str(error), error=True)
                return
            if not self._merge_context_matches(context) or isinstance(self.screen, ModalScreen):
                return

            def selected(parent: str | None) -> None:
                if parent and self._merge_context_matches(context):
                    self.request_parent(metadata.commit, chosen_path, parent)
                elif parent is None and self._merge_context_matches(context):
                    self.cancel_merge_requests()

            current = self.comparison.parent_commit if self.comparison and self.comparison.commit == metadata.commit else ""
            self.push_screen(ParentPickerScreen(metadata, parents, current=current), selected)

        self.run_worker(show, group="merge-picker", exclusive=True, exit_on_error=False)

    @staticmethod
    def parent_argument(value: str) -> int | str:
        if value.isascii() and value.isdecimal() and len(value) <= 9:
            number = int(value)
            if number > 0:
                return number
        if len(value) in (40, 64) and all(character in "0123456789abcdefABCDEF" for character in value):
            return value.lower()
        raise TrailError("Use :parent N (1-based) or an exact parent SHA; :parents opens the picker.")

    def choose_parent(self, parent: int | str) -> None:
        if not self._merge_allowed():
            return
        try:
            path = self.merge_path()
        except TrailError as error:
            self.status(str(error), error=True)
            return
        self.request_parent(None, path, parent)

    def action_nextparent(self) -> None:
        if self.comparison and self._merge_allowed():
            self.choose_parent(self.comparison.parent_index % len(self.comparison.parents) + 1)
        else:
            self.action_parents()

    def action_prevparent(self) -> None:
        if self.comparison and self._merge_allowed():
            self.choose_parent((self.comparison.parent_index - 2) % len(self.comparison.parents) + 1)
        else:
            self.action_parents()

    def prepare_comparison(self, repository, commit: str, path: str, parent: int | str, mode: str) -> PreparedComparison:
        comparison = repository.parent_comparison(commit, path, parent)
        view = repository.parent_source(comparison, mode == "before")
        canonical = repository.canonical_at(view.snapshot.commit if view.snapshot else comparison.commit)
        details = repository.canonical_at(comparison.commit).details(comparison.commit)
        return PreparedComparison(comparison, view, details, canonical, mode)

    def request_parent(self, commit: str | None, path: str, parent: int | str, mode: str | None = None) -> None:
        if not self._merge_allowed():
            return
        self.cancel_merge_requests()
        context = self._merge_context()
        repository = self.repository
        targets = self.parent_targets(commit, path)
        mode = mode or ("before" if self.source_mode == "before" else "after")
        base = self._comparison_base_visit or self.current_visit()
        if base is None:
            self.status("Wait for a committed source view before reviewing parents.", error=True)
            return

        async def review() -> None:
            try:
                chosen_commit = commit
                chosen_path = path
                if chosen_commit is None:
                    metadata, _, chosen_path = await asyncio.to_thread(self.prepare_parent_choices, repository, targets)
                    chosen_commit = metadata.commit
                prepared = await asyncio.to_thread(self.prepare_comparison, repository, chosen_commit, chosen_path, parent, mode)
            except TrailError as error:
                if self._merge_context_matches(context):
                    self._merge_pending_context = None
                    self.status(f"Could not compare merge parent: {error}", error=True)
                return
            if not self._merge_context_matches(context) or isinstance(self.screen, ModalScreen):
                return
            self.record_visit()
            self.begin_navigation()
            self._view_generation += 1
            self.workers.cancel_group(self, "view")
            self.workers.cancel_group(self, "diff-source")
            self.apply_comparison(prepared, base)
            self._navigation_ready = True
            self.record_visit()

        self.run_worker(review, group="merge-review", exclusive=True, exit_on_error=False)

    def apply_comparison(self, prepared: PreparedComparison, base: Visit) -> None:
        self.comparison = prepared.comparison
        self._comparison_entry = prepared.comparison.entry
        self._comparison_base_visit = base
        self.apply_comparison_view(prepared.view, prepared.details, prepared.repository, prepared.mode)

    def apply_comparison_view(self, view: HistoryView, details: CommitDetails, repository, mode: str) -> None:
        self.source_mode = mode
        self.snapshot = view.snapshot
        self.snapshot_repository = repository
        for value in ("before", "after", "starting"):
            self.query_one(f"#view-{value}", Button).set_class(value == mode, "active")
        if view.snapshot is not None:
            self.display_snapshot(view.snapshot)
            if view.selection and view.snapshot.lines:
                self.select_range(view.selection)
        else:
            self.query_one("#source").load_text("")
            self.query_one("#source-title", Static).update("SOURCE · unavailable")
        self.query_one("#source-note", Static).update(Text(view.note))
        self.query_one("#source-note", Static).add_class("important")
        self.refresh_search()
        self.query_one("#diff-title", Static).update(Text(
            f"FULL-FILE · {self.comparison.commit[:10]} · P{self.comparison.parent_index}/{len(self.comparison.parents)} "
            f"{self.comparison.parent_commit[:10]}"
        ))
        self.render_diff()
        self.render_commit_context(self.review_entry, details)
        self.refresh_parent_control()
        self.status(self.comparison_footer(mode + (" · source unavailable" if view.snapshot is None else "")))
        self.call_after_refresh(self.position_diff)

    def comparison_footer(self, message: str) -> str:
        comparison = self.comparison
        if comparison is None:
            return message
        return (f"FULL-FILE {comparison.commit[:7]} · parent {comparison.parent_index}/{len(comparison.parents)} "
                f"{comparison.parent_commit[:7]} · {message}")

    def set_comparison_mode(self, mode: str) -> None:
        if mode == "starting":
            self.cancel_merge_requests()
            base = self._comparison_base_visit
            if base is not None:
                self.start_restore(replace(base, mode="starting", display_commit="", display_path="",
                                           display_start=0, display_end=0))
        elif self.comparison:
            self.request_parent(self.comparison.commit, self.comparison.path, self.comparison.parent_commit, mode)

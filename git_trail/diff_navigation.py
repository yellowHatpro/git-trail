from __future__ import annotations

import asyncio

from textual import events
from textual.screen import ModalScreen
from textual.widgets import Input, TabbedContent

from .git import HistoryView, TrailError


class DiffNavigationSupport:
    def cancel_diff_jump(self) -> None:
        pending = self._diff_request
        if pending and pending[0] == self._navigation_generation:
            ready = pending[1]
            self.begin_navigation()
            self._view_generation += 1
            self.workers.cancel_group(self, "diff-source")
            self._navigation_ready = ready

    def diff_navigation_allowed(self) -> bool:
        return not (
            isinstance(self.focused, Input) or isinstance(self.screen, ModalScreen)
            or (hasattr(self.focused, "vim") and self.focused.vim.mode != "NORMAL")
        )

    def diff_focused(self) -> bool:
        return (
            self.diff_navigation_allowed() and self.focused is self.query_one("#diff-scroll")
            and self.query_one("#review-tabs", TabbedContent).active == "diff-tab"
        )

    def diff_visible_row(self) -> int | None:
        return next((row for row, item in enumerate(self._diff_rows)
                     if self._diff_cursor in item.indices), None)

    def refresh_diff_layout(self) -> None:
        self.render_diff()
        if self._diff_active:
            self.call_after_refresh(self.reveal_diff_cursor)

    def reveal_diff_cursor(self) -> None:
        if not self.review_entry or not self._diff_active:
            return
        row = self.diff_visible_row()
        if row is None:
            return
        pane = self.query_one("#diff-scroll")
        height = max(1, pane.scrollable_content_region.height)
        if row < pane.scroll_y or row >= pane.scroll_y + height:
            pane.scroll_to(y=max(0, row - min(2, height - 1)), animate=False, immediate=True)

    def select_diff_index(self, index: int, side: str | None = None) -> None:
        if not self.review_entry or not 0 <= index < len(self._diff_patch.lines):
            return
        line = self._diff_patch.lines[index]
        chosen = side or (
            "before" if line.kind == "delete" or (line.kind == "context" and self.source_mode == "before")
            else "after"
        )
        if (index, chosen) != (self._diff_cursor, self._diff_side):
            self.cancel_diff_jump()
        self._diff_cursor = index
        self._diff_side = chosen
        self._diff_active = True
        self.refresh_diff_layout()
        number = line.old if self._diff_side == "before" else line.new
        path = self.review_entry.old_path if self._diff_side == "before" else self.review_entry.new_path
        self.status(self.comparison_footer(
            f"{self._diff_side.capitalize()} · {path}:{number} · Enter: source"
            if number is not None else "This diff row has no source line."
        ))

    def move_diff_cursor(self, delta: int = 0, *, edge: str | None = None) -> None:
        if not self.diff_focused() or not self.review_entry or not self._diff_rows:
            return
        rows = [row for row, item in enumerate(self._diff_rows) if item.indices]
        if not rows:
            return
        row = self.diff_visible_row()
        if edge:
            target = rows[0] if edge == "first" else rows[-1]
        else:
            row = row if row is not None else rows[0]
            target = max(rows[0], min(row + delta, rows[-1]))
        item = self._diff_rows[target]
        index = item.old if self._diff_side == "before" else item.new
        self.select_diff_index(index if index is not None else item.indices[0],
                               self._diff_side if self.diff_effective_layout == "split" else None)

    def diff_click(self, row: int, column: int) -> None:
        if not self.review_entry or not 0 <= row < len(self._diff_rows):
            return
        item = self._diff_rows[row]
        if not item.indices:
            return
        side = None
        if self.diff_effective_layout == "split":
            side = "before" if column < self._diff_separator else "after"
            index = item.old if side == "before" else item.new
        else:
            index = item.indices[0]
        self.select_diff_index(index if index is not None else item.indices[0], side)
        self.query_one("#diff-scroll").focus()

    def next_diff_change(self, delta: int) -> None:
        if not self.diff_navigation_allowed() or self.review_entry is None:
            return
        targets = self._diff_patch.change_targets()
        if not targets:
            self.status(self.comparison_footer("No navigable source changes in this diff."))
            return
        if not self._diff_active or self._diff_cursor is None:
            target = targets[0 if delta > 0 else -1]
        elif delta > 0:
            target = next((item for item in targets if item > self._diff_cursor), targets[0])
        else:
            target = next((item for item in reversed(targets) if item < self._diff_cursor), targets[-1])
        self.query_one("#review-tabs", TabbedContent).active = "diff-tab"
        self.select_diff_index(target)
        self.query_one("#diff-scroll").focus()
        self.status(self.comparison_footer(
            f"Change {targets.index(target) + 1}/{len(targets)} · Enter: historical source"
        ))

    def action_nextchange(self) -> None:
        self.next_diff_change(1)

    def action_prevchange(self) -> None:
        self.next_diff_change(-1)

    def action_diffsource(self) -> None:
        self.jump_diff_source()

    def handle_diff_key(self, event: events.Key) -> bool:
        if not self.diff_focused():
            return False
        key = event.character or event.key
        prefix = self._normal_prefix
        if prefix in ("diff-next", "diff-prev"):
            self._normal_prefix = ""
            if key == "c":
                self.next_diff_change(1 if prefix == "diff-next" else -1)
            else:
                self.status("Sequence cancelled · ]c/[c: change")
        elif prefix in ("", "diff-g") and key in ("j", "k", "g", "G", "[", "]"):
            self._normal_prefix = ""
            if key in ("[", "]"):
                self._normal_prefix = "diff-next" if key == "]" else "diff-prev"
                self.status(f"{key}c: {'next' if key == ']' else 'previous'} change")
            elif key == "g" and prefix != "diff-g":
                self._normal_prefix = "diff-g"
            elif key in ("g", "G"):
                self.move_diff_cursor(edge="first" if key == "g" else "last")
            else:
                self.move_diff_cursor(1 if key == "j" else -1)
        else:
            return False
        event.prevent_default()
        event.stop()
        return True

    def jump_diff_source(self, side: str | None = None) -> None:
        if not self.diff_navigation_allowed() or self.review_entry is None or self._diff_cursor is None:
            return
        entry = self.review_entry
        comparison = self.comparison
        index = self._diff_cursor
        chosen = side or self._diff_side or ("before" if self._diff_patch.lines[index].kind == "delete" else "after")
        if side and self.diff_effective_layout == "split":
            row = self.diff_visible_row()
            item = self._diff_rows[row] if row is not None else None
            index = (item.old if chosen == "before" else item.new) if item else None
        line = self._diff_patch.lines[index] if index is not None else None
        number = (line.old if chosen == "before" else line.new) if line else None
        if number is None:
            self.status("No source line on this side of the diff row.", error=True)
            return
        previous_ready, settled_leaving = self.navigation_context()
        leaving_index, leaving = settled_leaving if settled_leaving else (self.journal.index, None)
        source_selection = self.query_one("#source").selection
        generation = self.begin_navigation()
        self._diff_request = generation, previous_ready, leaving, leaving_index
        self._diff_source_selection = source_selection
        self._view_generation += 1
        view_generation = self._view_generation
        self.workers.cancel_group(self, "view")
        repository = self._timeline_repository if self.timeline else self.repository

        def prepare():
            if comparison is not None:
                view = repository.parent_diff_source(comparison, number, chosen == "before")
                details = repository.canonical_at(entry.commit).details(entry.commit)
                source_repository = repository.canonical_at(view.snapshot.commit)
            else:
                view = repository.diff_source(entry, number, chosen == "before")
                details = repository.details(entry.commit)
                source_repository = repository.at(view.snapshot.commit)
            return view.snapshot, view.selection, view.note, details, source_repository

        async def jump() -> None:
            try:
                prepared = await asyncio.to_thread(prepare)
            except TrailError as error:
                if generation == self._navigation_generation and view_generation == self._view_generation:
                    self._diff_request = None
                    self._navigation_ready = previous_ready
                    self.status(f"Could not open diff source: {error}", error=True)
                return
            if (generation != self._navigation_generation or view_generation != self._view_generation
                    or self.review_entry is not entry or self.comparison is not comparison):
                return
            if self.query_one("#source").selection != source_selection:
                self.cancel_diff_jump()
                return
            if isinstance(self.screen, ModalScreen):
                self.cancel_diff_jump()
                return
            if leaving is not None and 0 <= leaving_index < len(self.journal.visits):
                self.journal.visits[leaving_index] = leaving
            self._diff_request = None
            self.source_mode = chosen
            for mode in ("before", "after", "starting"):
                self.query_one(f"#view-{mode}").set_class(mode == chosen, "active")
            if comparison is not None:
                self.apply_comparison_view(HistoryView(*prepared[:3]), prepared[3], prepared[4], chosen)
                self._navigation_ready = True
                self.record_visit()
            else:
                await self.load_entry(entry, view_generation, chosen, prepared=prepared)
                self.status(prepared[2])
            if not self.query_one("#prompt-row").display and not isinstance(self.focused, Input):
                self.query_one("#source").focus()

        self.run_worker(jump, group="diff-source", exclusive=True, exit_on_error=False)

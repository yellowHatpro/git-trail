from __future__ import annotations

import re
import asyncio
import threading
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from .git import LineRange, TrailError
from .filters import HistoryFilter


@dataclass(frozen=True)
class Visit:
    repository: str
    commit: str
    path: str
    start: int
    end: int
    follow: bool = False
    traced: bool = False
    selected_commit: str = ""
    mode: str = "starting"
    history_filter: str = ""
    display_commit: str = ""
    display_path: str = ""
    display_start: int = 0
    display_end: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict):
        try:
            if not isinstance(value, dict):
                raise ValueError("expected an object")
            result = cls(**value)
            if not isinstance(result.repository, str) or not result.repository.isprintable() or not Path(result.repository).is_absolute():
                raise ValueError("repository must be absolute")
            if not isinstance(result.commit, str) or not re.fullmatch(r"[0-9a-f]{40,64}", result.commit):
                raise ValueError("commit must be a resolved SHA")
            if not isinstance(result.selected_commit, str) or (result.selected_commit and not re.fullmatch(r"[0-9a-f]{40,64}", result.selected_commit)):
                raise ValueError("selected commit must be a resolved SHA")
            if not isinstance(result.path, str) or not result.path or result.path == "." or PurePosixPath(result.path).is_absolute() or ".." in PurePosixPath(result.path).parts or any(char in result.path for char in "\0\n\r"):
                raise ValueError("path must be repository-relative")
            if type(result.start) is not int or type(result.end) is not int or not 1 <= result.start <= result.end <= 2_000_000_000:
                raise ValueError("invalid line range")
            if type(result.follow) is not bool or type(result.traced) is not bool:
                raise ValueError("invalid tracking flags")
            if result.mode not in ("after", "before", "starting") or not isinstance(result.history_filter, str) or len(result.history_filter) > 10_000:
                raise ValueError("invalid view settings")
            if not isinstance(result.display_commit, str) or not isinstance(result.display_path, str):
                raise ValueError("invalid displayed source")
            if result.display_commit:
                cls.from_dict(dict(repository=result.repository, commit=result.display_commit,
                                   path=result.display_path, start=result.display_start, end=result.display_end))
            elif result.display_path or type(result.display_start) is not int or type(result.display_end) is not int or result.display_start or result.display_end:
                raise ValueError("incomplete displayed source")
            if result.follow and not result.traced or result.selected_commit and not result.traced:
                raise ValueError("history settings require a traced selection")
            if not result.traced and (result.mode != "starting" or result.history_filter):
                raise ValueError("untraced selections use the starting source without a history filter")
            return result
        except (ValueError, TypeError) as error:
            raise TrailError(f"Invalid saved visit: {error}.") from error


class Journal:
    def __init__(self):
        self.visits: list[Visit] = []
        self.index = -1

    def record(self, visit: Visit) -> None:
        if self.index >= 0 and self.visits[self.index] == visit:
            return
        del self.visits[self.index + 1:]
        self.visits.append(visit)
        if len(self.visits) > 200:
            self.visits.pop(0)
        self.index = len(self.visits) - 1

    def target(self, delta: int) -> tuple[int, Visit]:
        index = self.index + delta
        if not 0 <= index < len(self.visits):
            raise TrailError("No earlier visit." if delta < 0 else "No later visit.")
        return index, self.visits[index]


def bookmark_name(value: str) -> str:
    if not isinstance(value, str):
        raise TrailError("Use a bookmark name with 1 to 80 visible characters.")
    name = value.strip()
    if not name or len(name) > 80 or not name.isprintable():
        raise TrailError("Use a bookmark name with 1 to 80 visible characters.")
    return name


class NavigationSupport:
    def init_navigation(self) -> None:
        self.journal = Journal()
        self.navigation_data = {"sessions": {}, "bookmarks": {}}
        self._navigation_generation = 0
        self._navigation_ready = False
        self._restoring = False
        self._restore_previous_ready = False
        self._navigation_target_index = None
        self._restore_leaving = None

    @property
    def repository_key(self) -> str:
        return str(self.repository.root.resolve())

    def begin_navigation(self) -> int:
        self._navigation_generation += 1
        self._diff_request = None
        self._restoring = False
        self._navigation_target_index = None
        self._restore_leaving = None
        self._navigation_ready = False
        self._symbol_cancel.set()
        return self._navigation_generation

    def navigation_context(self):
        if self._restoring:
            return self._restore_previous_ready, self._restore_leaving
        pending = getattr(self, "_diff_request", None)
        if pending and pending[0] == self._navigation_generation:
            _, ready, visit, index = pending
            return ready, (index, visit) if visit is not None else None
        current = self.current_visit()
        return self._navigation_ready, (self.journal.index, current) if current else None

    def current_visit(self) -> Visit | None:
        if not self._navigation_ready:
            return None
        anchor = self.traced_snapshot if self.timeline else self.snapshot
        if anchor is None or not anchor.lines:
            return None
        selection = self.timeline.selection if self.timeline else self.selected_range()
        display = {}
        if self.snapshot and self.snapshot.lines:
            current = self.selected_range()
            display = dict(display_commit=self.snapshot.commit, display_path=self.snapshot.path,
                           display_start=current.start, display_end=current.end)
        return Visit(
            self.repository_key, anchor.commit, anchor.path, selection.start, selection.end,
            follow=bool(self.timeline and self.timeline.mode == "symbol"),
            traced=self.timeline is not None,
            selected_commit=self.selected_entry.commit if self.selected_entry else "",
            mode=self.source_mode if self.timeline else "starting",
            history_filter=self._valid_history_filter if self.timeline else "",
            **display,
        )

    def record_visit(self) -> None:
        if self._restoring:
            return
        visit = self.current_visit()
        if visit is not None:
            self.journal.record(visit)
            self.navigation_data["sessions"][self.repository_key] = visit.to_dict()

    def saved_bookmarks(self) -> dict:
        items = self.navigation_data["bookmarks"].get(self.repository_key, {})
        if not isinstance(items, dict):
            raise TrailError("Invalid saved bookmarks for this repository.")
        return items

    def save_bookmark(self, name: str) -> None:
        name = bookmark_name(name)
        visit = self.current_visit()
        if visit is None:
            raise TrailError("Wait for a source selection or history view before bookmarking.")
        items = self.saved_bookmarks()
        if name not in items and len(items) >= 100:
            raise TrailError("Save at most 100 bookmarks per repository.")
        items[name] = Visit.from_dict(visit.to_dict()).to_dict()
        self.navigation_data["bookmarks"][self.repository_key] = items
        self.status(f"Bookmarked {name}. Saved on exit.")

    def delete_bookmark(self, name: str) -> None:
        name = bookmark_name(name)
        items = self.saved_bookmarks()
        if name not in items:
            raise TrailError(f"No bookmark named {name}.")
        del items[name]
        self.status(f"Removed bookmark {name}. Saved on exit.")

    def open_bookmark(self, name: str) -> None:
        name = bookmark_name(name)
        items = self.saved_bookmarks()
        if name not in items:
            raise TrailError(f"No bookmark named {name}.")
        self.start_restore(Visit.from_dict(items[name]))

    def start_restore(self, visit: Visit, index: int | None = None, *, leaving=None) -> None:
        previous_ready, settled_leaving = self.navigation_context()
        if leaving is None:
            leaving = settled_leaving
        generation = self.begin_navigation()
        self._restoring = True
        self._restore_previous_ready = previous_ready
        self._navigation_target_index = index
        self._restore_leaving = leaving
        request = generation, previous_ready, leaving

        async def restore() -> None:
            await self.restore_visit(visit, index, request=request)

        self.run_worker(restore, group="navigation", exclusive=True, exit_on_error=False)

    def action_back(self) -> None:
        self.move_visit(-1)

    def action_forward(self) -> None:
        self.move_visit(1)

    def move_visit(self, delta: int) -> None:
        try:
            base = self._navigation_target_index if self._restoring and self._navigation_target_index is not None else self.journal.index
            index = base + delta
            if not 0 <= index < len(self.journal.visits):
                raise TrailError("No earlier visit." if delta < 0 else "No later visit.")
            _, leaving = self.navigation_context()
            visit = leaving[1] if leaving and index == leaving[0] else self.journal.visits[index]
            self.start_restore(visit, index, leaving=leaving)
        except TrailError as error:
            self.status(str(error), error=True)

    def prepare_visit(self, visit: Visit, cancellation: threading.Event):
        visit = Visit.from_dict(visit.to_dict())
        if Path(visit.repository).resolve() != self.repository.root.resolve():
            raise TrailError("Saved visit belongs to a different repository.")
        predicate = HistoryFilter.parse(visit.history_filter)
        repository = self.repository.at(visit.commit)
        if self.store:
            self.store.allow_repository(repository.root)
        anchor = repository.snapshot(visit.path)
        selection = LineRange(visit.start, visit.end, "saved selection")
        selection.validate(len(anchor.lines))
        timeline = None
        entry = None
        prepared = None
        if visit.traced:
            timeline = (repository.symbol_history(anchor, selection, cancel=cancellation)
                        if visit.follow else repository.history(anchor, selection))
            entries = tuple(item for item in timeline.entries if predicate.matches(item))
            if visit.selected_commit:
                entry = next((item for item in entries if item.commit == visit.selected_commit), None)
                if entry is None:
                    raise TrailError("Saved change is unavailable in this selection's timeline.")
            elif entries:
                raise TrailError("Saved timeline does not specify its selected change.")
            if entry:
                if visit.mode == "starting":
                    snapshot, selected, note = anchor, selection, "Starting source for this timeline"
                else:
                    view = repository.historical_source(entry, visit.mode == "before")
                    snapshot, selected, note = view.snapshot, view.selection, view.note
                details = repository.details(entry.commit)
                source_repository = repository.at(snapshot.commit) if snapshot else repository
                prepared = snapshot, selected, note, details, source_repository
            else:
                snapshot = anchor if not timeline.entries else None
        else:
            snapshot = anchor
        if visit.display_commit:
            if snapshot is None or (snapshot.commit, snapshot.path) != (visit.display_commit, visit.display_path):
                raise TrailError("Saved displayed source does not match its historical view.")
            LineRange(visit.display_start, visit.display_end).validate(len(snapshot.lines))
        return repository, anchor, selection, timeline, entry, prepared, repository.files()

    async def restore_visit(self, visit: Visit, index: int | None = None, *, request=None) -> bool:
        if request is None:
            previous_ready, leaving = self.navigation_context()
            generation = self.begin_navigation()
        else:
            generation, previous_ready, leaving = request
            if generation != self._navigation_generation:
                return False
        self._restoring = True
        self._restore_previous_ready = previous_ready
        self._load_generation += 1
        self._history_generation += 1
        self._view_generation += 1
        for group in ("file", "history", "view"):
            self.workers.cancel_group(self, group)
        cancellation = threading.Event()
        self._symbol_cancel = cancellation
        self.status("Restoring saved selection…")
        try:
            prepared_visit = await asyncio.to_thread(self.prepare_visit, visit, cancellation)
        except asyncio.CancelledError:
            cancellation.set()
            raise
        except TrailError as error:
            if generation == self._navigation_generation:
                self._restoring = False
                self._navigation_target_index = None
                self._restore_leaving = None
                self._navigation_ready = previous_ready
                self.status(f"Could not restore: {error}", error=True)
            return False
        if generation != self._navigation_generation:
            return False
        repository, anchor, selection, timeline, entry, prepared, files = prepared_visit
        self.repository = repository
        self.traced_snapshot = anchor
        self.snapshot_repository = repository
        self.snapshot = anchor
        self.timeline = timeline
        self.selected_entry = entry
        self.source_mode = visit.mode
        self.all_files = files
        self.filter_files(self.query_one("#file-filter").value)
        field = self.query_one("#history-filter")
        with field.prevent(type(field).Changed):
            field.value = visit.history_filter
        self.query_one("#workspace-title").update(f"Git Trail  /  {repository.root.name}  /  {repository.commit[:7]}")
        self.sub_title = f"{repository.root.name} · {repository.commit[:10]}"
        for mode in ("after", "before", "starting"):
            self.query_one(f"#view-{mode}").set_class(mode == visit.mode, "active")
        if timeline:
            self.apply_history_filter(load_view=False)
            if entry:
                self.query_one("#diff-title").update(f"{entry.commit[:10]} · {entry.author} · committed {entry.committed_at}")
                self.render_diff()
                self.call_after_refresh(self.position_diff)
                await self.load_entry(entry, self._view_generation, visit.mode, prepared=prepared)
            elif not timeline.entries:
                self.snapshot = anchor
                self.display_snapshot(anchor)
                self.select_range(selection)
                self.query_one("#source-note").update("\n".join(timeline.warnings))
                self.query_one("#source-note").add_class("important")
                self.query_one("#diff").update("\n".join(timeline.warnings))
        else:
            self.filtered_entries = ()
            self.query_one("#timeline").clear_options()
            self.query_one("#timeline-title").update("TIMELINE")
            self.query_one("#diff").update("Choose a selection in the committed source.")
            self.query_one("#diff-title").update("SELECTED-RANGE DIFF")
            self.query_one("#commit-details").update("Choose a file and trace its source.")
            self.display_snapshot(anchor)
            self.select_range(selection)
        if generation != self._navigation_generation:
            return False
        if visit.display_commit:
            self.select_range(LineRange(visit.display_start, visit.display_end, "saved selection"))
        self.close_prompt()
        self.query_one("#source").focus()
        self._restoring = False
        self._navigation_target_index = None
        self._restore_leaving = None
        self._navigation_ready = True
        if leaving is not None:
            self.journal.visits[leaving[0]] = leaving[1]
        if index is not None:
            self.journal.index = index
            self.navigation_data["sessions"][self.repository_key] = visit.to_dict()
        else:
            self.record_visit()
        self.status(f"Restored {visit.path}:{visit.start}:{visit.end} at {visit.commit[:10]}"
                    + (" · partial symbol history" if timeline and any("Partial" in item for item in timeline.warnings) else ""))
        return True

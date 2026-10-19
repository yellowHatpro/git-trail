from __future__ import annotations

import asyncio
import gc
import threading
import warnings
from dataclasses import replace

import pytest
from textual.widgets import Input, OptionList, Static

from git_trail.app import BookmarkScreen, InitialSelection, SourceView, TrailApp
from git_trail.cli import main
from git_trail.git import LineRange, Repository, TrailError
from git_trail.navigation import Journal, Visit, bookmark_name
from git_trail.state import StateStore


async def settle(app, pilot):
    async def idle():
        for _ in range(10):
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            if not app.workers:
                return
        pytest.fail("Navigation did not reach an idle UI state.")

    await asyncio.wait_for(idle(), timeout=15)


def content(app, selector):
    value = app.query_one(selector, Static).content
    return value.plain if hasattr(value, "plain") else str(value)


async def command(app, pilot, value):
    await pilot.press(":")
    field = app.query_one("#command-input", Input)
    assert app.focused is field
    field.value = value
    await pilot.press("enter")
    await settle(app, pilot)


def scene(app):
    source = app.query_one("#source", SourceView)
    return (
        app.repository.commit, app.snapshot, app.traced_snapshot, app.timeline,
        app.selected_entry, app.source_mode, source.text, source.selection,
        tuple(entry.commit for entry in app.filtered_entries),
        app.query_one("#history-filter", Input).value,
        content(app, "#workspace-title"), content(app, "#diff-title"),
    )


def sample_visit(**values):
    return Visit.from_dict({
        "repository": "/tmp/example-repository",
        "commit": "a" * 40, "path": "src/module.py", "start": 3, "end": 7,
        **values,
    })


def test_visit_round_trip_keeps_anchor_and_display_coordinates_distinct():
    visit = sample_visit(
        traced=True, follow=True, selected_commit="b" * 40,
        mode="before", history_filter="author:Jane",
        display_commit="c" * 40, display_path="older/module.py",
        display_start=9, display_end=10,
    )
    assert Visit.from_dict(visit.to_dict()) == visit
    assert (visit.start, visit.end) == (3, 7)
    assert (visit.display_start, visit.display_end) == (9, 10)
    assert visit.path != visit.display_path


@pytest.mark.parametrize(
    "invalid",
    [
        {"repository": "relative"},
        {"repository": "/tmp/example\0bad"},
        {"repository": "/tmp/example\nbad"},
        {"commit": "--all"},
        {"path": "/etc/passwd"},
        {"path": "../escape.py"},
        {"path": "line\nbreak.py"},
        {"start": True},
        {"end": 0},
        {"follow": True},
        {"traced": "true"},
        {"selected_commit": "b" * 40},
        {"mode": "before"},
        {"history_filter": "author:Jane"},
        {"history_filter": "x" * 10_001},
        {"display_path": "incomplete.py"},
        {"display_commit": "c" * 40, "display_path": "../escape.py", "display_start": 1, "display_end": 2},
        {"display_commit": "c" * 40, "display_path": "good.py", "display_start": True, "display_end": 2},
        {"unknown": "field"},
    ],
)
def test_saved_visit_schema_rejects_unsafe_or_inconsistent_records(invalid):
    with pytest.raises(TrailError, match="Invalid saved visit"):
        sample_visit(**invalid)


def test_journal_deduplicates_truncates_forward_history_and_bounds_navigation():
    journal = Journal()
    first, second, third = [sample_visit(start=line, end=line) for line in (1, 2, 3)]
    journal.record(first)
    journal.record(first)
    assert journal.visits == [first]
    journal.record(second)
    journal.record(third)
    assert journal.target(-1) == (1, second)
    journal.index = 1
    replacement = sample_visit(commit="d" * 40)
    journal.record(replacement)
    assert journal.visits == [first, second, replacement]
    with pytest.raises(TrailError, match="No later"):
        journal.target(1)
    journal = Journal()
    for line in range(1, 206):
        journal.record(sample_visit(start=line, end=line))
    assert len(journal.visits) == 200
    assert journal.visits[0].start == 6
    assert journal.index == 199
    journal.index = 0
    with pytest.raises(TrailError, match="No earlier"):
        journal.target(-1)


@pytest.mark.parametrize("value", ["", " ", "x" * 81, "bad\nname", "bad\0name", 123, None])
def test_bookmark_names_reject_invalid_or_nontext_values(value):
    with pytest.raises(TrailError):
        bookmark_name(value)


@pytest.fixture
def navigation_files(repo):
    for name in ("first", "second", "third"):
        repo.write(f"{name}.txt", f"{name} one\n{name} two\n{name} three\n")
    repo.commit("Navigation choices")
    return repo


async def test_back_forward_keys_and_leader_restore_last_selection_and_truncate_after_new_visit(navigation_files):
    repo = navigation_files
    before = repo.state()
    app = TrailApp(Repository(repo.root), InitialSelection("first.txt"))
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        source = app.query_one("#source", SourceView)
        source.move_cursor((1, 0))
        await app.load_file("second.txt")
        await settle(app, pilot)
        source.move_cursor((2, 0))
        await pilot.press("alt+left")
        await settle(app, pilot)
        assert app.snapshot.path == "first.txt"
        assert app.selected_range().start == app.selected_range().end == 2
        await pilot.press("alt+right")
        await settle(app, pilot)
        assert app.snapshot.path == "second.txt"
        assert app.selected_range().start == app.selected_range().end == 3
        await pilot.press("space", "h", "b")
        await settle(app, pilot)
        assert app.snapshot.path == "first.txt"
        await pilot.press("space", "h", "f")
        await settle(app, pilot)
        assert app.snapshot.path == "second.txt"
        await command(app, pilot, "back")
        assert app.snapshot.path == "first.txt"
        await app.load_file("third.txt")
        await settle(app, pilot)
        current = scene(app)
        index = app.journal.index
        await command(app, pilot, "forward")
        assert "No later visit" in content(app, "#status")
        assert app.journal.index == index
        assert scene(app) == current
    assert repo.state() == before


async def test_history_navigation_restores_filter_mode_entry_and_a_displayed_subrange(range_history):
    fixture = range_history
    app = TrailApp(Repository(fixture.repo.root), InitialSelection(fixture.path, LineRange(4, 7)))
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        query = f"hash:{fixture.insert[:10]}"
        await command(app, pilot, f"filter {query}")
        assert app.selected_entry.commit == fixture.insert
        assert app.snapshot.commit == fixture.insert
        await command(app, pilot, "before")
        assert app.snapshot.commit == fixture.unrelated
        app.select_range(LineRange(5, 6))
        await pilot.pause()
        await pilot.press("alt+left")
        await settle(app, pilot)
        assert app.source_mode == "after"
        assert app.selected_entry.commit == fixture.insert
        assert app.snapshot.commit == fixture.insert
        assert app.query_one("#history-filter", Input).value == query
        await pilot.press("alt+left")
        await settle(app, pilot)
        assert app.source_mode == "after"
        assert app.selected_entry.commit == fixture.delete
        assert app.query_one("#history-filter", Input).value == ""
        assert [entry.commit for entry in app.filtered_entries] == fixture.relevant
        await pilot.press("alt+right")
        await settle(app, pilot)
        await pilot.press("alt+right")
        await settle(app, pilot)
        assert app.source_mode == "before"
        assert app.selected_entry.commit == fixture.insert
        assert app.snapshot.commit == fixture.unrelated
        assert app.query_one("#history-filter", Input).value == query
        assert (app.selected_range().start, app.selected_range().end) == (5, 6)


async def test_bookmark_picker_commands_and_navigation_guards_keep_repository_scoped_selections(navigation_files):
    repo = navigation_files
    before = repo.state()
    app = TrailApp(Repository(repo.root), InitialSelection("first.txt"))
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        source = app.query_one("#source", SourceView)
        source.move_cursor((1, 0))
        await command(app, pilot, "bookmark First note")
        await app.load_file("second.txt")
        await settle(app, pilot)
        source.move_cursor((2, 0))
        await command(app, pilot, "bookmark [Second note]")
        assert set(app.saved_bookmarks()) == {"First note", "[Second note]"}
        await pilot.press("ctrl+f")
        field = app.query_one("#find-input", Input)
        current = scene(app)
        index = app.journal.index
        await pilot.press("alt+left", "alt+right")
        assert app.focused is field
        assert app.journal.index == index
        assert scene(app) == current
        await pilot.press("escape")
        await command(app, pilot, "bookmarks")
        assert isinstance(app.screen, BookmarkScreen)
        field = app.screen.query_one("#symbol-filter", Input)
        options = app.screen.query_one("#symbol-options", OptionList)
        assert options.option_count == 2
        await pilot.press("ctrl+n", "ctrl+p")
        assert app.focused is field
        assert options.highlighted == 0
        await pilot.press("alt+left", "alt+right")
        assert app.journal.index == index
        assert isinstance(app.screen, BookmarkScreen)
        await pilot.press(*"second.txt")
        await pilot.pause()
        assert field.value == "second.txt"
        assert options.option_count == 1
        await pilot.press("enter")
        await settle(app, pilot)
        assert not isinstance(app.screen, BookmarkScreen)
        assert app.snapshot.path == "second.txt"
        assert app.selected_range().start == app.selected_range().end == 3
        await command(app, pilot, "open First note")
        assert app.snapshot.path == "first.txt"
        assert app.selected_range().start == app.selected_range().end == 2
        await command(app, pilot, "unbookmark First note")
        assert set(app.saved_bookmarks()) == {"[Second note]"}
        await pilot.press("space", "h", "m")
        await pilot.pause()
        assert isinstance(app.screen, BookmarkScreen)
        await pilot.press("escape")
        await pilot.pause()
    assert repo.state() == before


async def test_bookmark_limit_allows_replacement_and_bad_saved_names_leave_current_source_intact(navigation_files):
    app = TrailApp(Repository(navigation_files.root), InitialSelection("first.txt"))
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        for index in range(100):
            app.save_bookmark(f"Bookmark {index}")
        original = dict(app.saved_bookmarks())
        with pytest.raises(TrailError, match="100"):
            app.save_bookmark("Overflow")
        assert app.saved_bookmarks() == original
        app.query_one("#source", SourceView).move_cursor((1, 0))
        app.save_bookmark("Bookmark 0")
        assert len(app.saved_bookmarks()) == 100
        assert app.saved_bookmarks()["Bookmark 0"]["display_start"] == 2
        app.navigation_data["bookmarks"][app.repository_key] = {"bad\nname": app.current_visit().to_dict()}
        current = scene(app)
        await command(app, pilot, "bookmarks")
        assert not isinstance(app.screen, BookmarkScreen)
        assert "bookmark name" in content(app, "#status").casefold()
        assert scene(app) == current


async def test_opening_a_bookmark_then_back_restores_the_latest_leaving_source_selection(navigation_files):
    repo = navigation_files
    before = repo.state()
    app = TrailApp(Repository(repo.root), InitialSelection("first.txt"))
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        app.select_range(LineRange(2, 3))
        await pilot.pause()
        await command(app, pilot, "bookmark First subrange")
        await app.load_file("second.txt")
        await settle(app, pilot)
        app.select_range(LineRange(2, 3))
        await pilot.pause()
        leaving = app.current_visit()
        await command(app, pilot, "open First subrange")
        assert app.snapshot.path == "first.txt"
        assert (app.selected_range().start, app.selected_range().end) == (2, 3)
        await command(app, pilot, "back")
        assert app.snapshot.path == "second.txt"
        assert (app.selected_range().start, app.selected_range().end) == (2, 3)
        assert app.current_visit() == leaving
        await command(app, pilot, "forward")
        assert app.snapshot.path == "first.txt"
        assert (app.selected_range().start, app.selected_range().end) == (2, 3)
    assert repo.state() == before


async def test_queued_restores_cancelled_before_start_do_not_leak_unawaited_coroutines(navigation_files):
    app = TrailApp(Repository(navigation_files.root), InitialSelection("first.txt"))
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        visit = app.current_visit()
        index = app.journal.index
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", RuntimeWarning)
            app.start_restore(visit, index)
            app.start_restore(visit, index)
            app.start_restore(visit, index)
            await settle(app, pilot)
            gc.collect()
            assert not [
                item for item in caught
                if issubclass(item.category, RuntimeWarning)
                and "restore_visit" in str(item.message)
                and "never awaited" in str(item.message)
            ]
        assert app.journal.index == index
        assert app.current_visit() == visit
        assert app.query_one("#source", SourceView).text == "first one\nfirst two\nfirst three\n"


async def test_failed_restore_validation_keeps_the_settled_scene_and_journal_unchanged(range_history):
    fixture = range_history
    app = TrailApp(Repository(fixture.repo.root), InitialSelection(fixture.path, LineRange(4, 7)))
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        good = app.current_visit()
        current = scene(app)
        index = app.journal.index
        visits = list(app.journal.visits)
        invalid = [
            replace(good, commit="f" * 40),
            replace(good, path="missing.py"),
            replace(good, start=999, end=999),
            replace(good, selected_commit=fixture.shift),
            replace(good, history_filter=f"hash:{fixture.origin[:10]}"),
            replace(good, display_path="unrelated.txt"),
            replace(good, display_start=999, display_end=999),
            replace(good, repository=str(fixture.repo.root.parent)),
        ]
        for visit in invalid:
            assert await app.restore_visit(visit, index=0) is False
            await settle(app, pilot)
            assert scene(app) == current
            assert app.journal.index == index
            assert app.journal.visits == visits
            assert app.current_visit() == good
            assert "Could not restore" in content(app, "#status")


async def test_slow_restore_cannot_replace_a_newer_file_load(navigation_files, monkeypatch):
    repo = navigation_files
    app = TrailApp(Repository(repo.root), InitialSelection("first.txt"))
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        visit = app.current_visit()
        original = app.prepare_visit
        started, released = threading.Event(), threading.Event()

        def delayed(value, cancellation):
            started.set()
            if not released.wait(timeout=10):
                raise RuntimeError("Test did not release the delayed restore")
            return original(value, cancellation)

        monkeypatch.setattr(app, "prepare_visit", delayed)
        task = asyncio.create_task(app.restore_visit(visit))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            await app.load_file("third.txt")
            await settle(app, pilot)
            released.set()
            assert await task is False
            await settle(app, pilot)
            assert app.snapshot.path == "third.txt"
            assert app.query_one("#source", SourceView).text == "third one\nthird two\nthird three\n"
            assert app.current_visit().path == "third.txt"
        finally:
            released.set()
            await task


async def test_invalid_filter_cancels_pending_restore_and_keeps_the_old_selection_bookmarkable(range_history, monkeypatch):
    fixture = range_history
    app = TrailApp(Repository(fixture.repo.root), InitialSelection(fixture.path, LineRange(4, 7)))
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        visit = app.current_visit()
        snapshot, entry = app.snapshot, app.selected_entry
        original = app.prepare_visit
        started, released = threading.Event(), threading.Event()

        def delayed(value, cancellation):
            started.set()
            if not released.wait(timeout=10):
                raise RuntimeError("Test did not release the delayed restore")
            return original(value, cancellation)

        monkeypatch.setattr(app, "prepare_visit", delayed)
        task = asyncio.create_task(app.restore_visit(visit))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            await pilot.press("ctrl+l")
            app.query_one("#history-filter", Input).value = "after:2024-02-30"
            await pilot.pause()
            assert "YYYY-MM-DD" in content(app, "#status")
            assert app.snapshot == snapshot and app.selected_entry == entry
            assert app.current_visit() == visit
            app.save_bookmark("Retained source")
            assert app.saved_bookmarks()["Retained source"] == visit.to_dict()
            released.set()
            assert await task is False
            await settle(app, pilot)
            assert app.current_visit() == visit
        finally:
            released.set()
            await task


async def test_bookmark_restores_before_source_from_an_old_file_after_a_symbol_move(repo):
    old = (
        "# original source\n\n"
        "def original(values):\n"
        "    positive = [value for value in values if value > 0]\n"
        "    total = sum(positive)\n"
        "    count = len(positive)\n"
        "    average = total / max(count, 1)\n"
        "    return round(average, 2)\n"
    )
    repo.write("old.py", old)
    origin = repo.commit("Original function")
    (repo.root / "old.py").unlink()
    repo.write("new.py", old.split("\n\n", 1)[1].replace("original", "renamed"))
    repo.write("notes.txt", "another source\n")
    move = repo.commit("Move and rename function")
    before = repo.state()
    app = TrailApp(Repository(repo.root), InitialSelection("new.py", LineRange(1, 6), follow_symbol=True))
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        await command(app, pilot, "before")
        assert app.snapshot.path == "old.py" and app.snapshot.commit == origin
        app.select_range(LineRange(4, 5))
        await pilot.pause()
        await command(app, pilot, "bookmark Old implementation")
        saved = app.current_visit()
        assert saved.follow and saved.traced
        assert saved.commit == move and saved.path == "new.py"
        assert saved.display_commit == origin and saved.display_path == "old.py"
        await app.load_file("notes.txt")
        await settle(app, pilot)
        await command(app, pilot, "open Old implementation")
        assert app.timeline.mode == "symbol"
        assert app.selected_entry.commit == move
        assert app.source_mode == "before"
        assert app.snapshot.path == "old.py" and app.snapshot.commit == origin
        assert app.traced_snapshot.path == "new.py" and app.traced_snapshot.commit == move
        assert (app.selected_range().start, app.selected_range().end) == (4, 5)
        assert app.current_visit() == saved
    assert repo.state() == before


async def test_last_session_and_bookmark_restore_captured_commit_after_head_changes(range_history, tmp_path):
    fixture = range_history
    path = tmp_path / "user-state" / "state.json"
    app = TrailApp(Repository(fixture.repo.root), InitialSelection(fixture.path, LineRange(4, 7)), store=StateStore(path))
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        query = f"hash:{fixture.insert[:10]}"
        await command(app, pilot, f"filter {query}")
        await command(app, pilot, "before")
        app.select_range(LineRange(5, 6))
        await pilot.pause()
        await command(app, pilot, "bookmark Saved filtered source")
        saved = app.current_visit()
        await pilot.press("ctrl+q")
    fixture.repo.write(fixture.path, "def future():\n    return 900\n")
    future = fixture.repo.commit("Replace present source")
    before = fixture.repo.state()
    app = TrailApp(Repository(fixture.repo.root), store=StateStore(path))
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        assert app.repository.commit == fixture.delete
        assert app.snapshot.commit == fixture.unrelated
        assert app.selected_entry.commit == fixture.insert
        assert app.source_mode == "before"
        assert app.query_one("#history-filter", Input).value == query
        assert (app.selected_range().start, app.selected_range().end) == (5, 6)
        assert app.current_visit() == saved
        assert "Saved filtered source" in app.saved_bookmarks()
        assert future != app.repository.commit
        await pilot.press("ctrl+q")
    assert fixture.repo.state() == before


@pytest.mark.parametrize("initial", [InitialSelection(resume=False), InitialSelection("first.txt")])
async def test_no_resume_and_explicit_file_do_not_replace_the_requested_start(navigation_files, tmp_path, initial):
    repo = navigation_files
    store = StateStore(tmp_path / "state.json")
    repository = Repository(repo.root)
    saved = Visit(str(repo.root.resolve()), repository.commit, "second.txt", 2, 2)
    store.data["sessions"][str(repo.root.resolve())] = saved.to_dict()
    app = TrailApp(repository, initial, store=store)
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(app, pilot)
        if initial.path:
            assert app.snapshot.path == "first.txt"
            assert app.current_visit().path == "first.txt"
        else:
            assert app.snapshot is None
            assert app.focused is app.query_one("#file-filter", Input)
        assert app.repository.commit == repository.commit
        await pilot.press("ctrl+q")


@pytest.mark.parametrize(
    ("arguments", "resume"),
    [([], True), (["--no-resume"], False), (["--ref", "HEAD"], False), (["--ref", "HEAD~1"], False)],
)
def test_cli_default_resume_and_explicit_ref_opt_out_are_passed_to_the_tui(repo, monkeypatch, arguments, resume):
    import git_trail.app

    repo.write("main.txt", "first\n")
    repo.commit("First")
    repo.write("main.txt", "second\n")
    repo.commit("Second")
    opened = []

    class CapturingApp:
        def __init__(self, repository, initial):
            self.initial = initial

        def run(self):
            opened.append(self.initial)

    monkeypatch.setattr(git_trail.app, "TrailApp", CapturingApp)
    assert main([str(repo.root), *arguments]) == 0
    assert len(opened) == 1
    assert opened[0].resume is resume

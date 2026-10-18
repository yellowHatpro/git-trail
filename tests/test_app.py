from __future__ import annotations

import asyncio
import threading

import pytest
from rich.syntax import Syntax
from textual.widgets import Input, OptionList, Static

from git_trail.app import InitialSelection, JumpScreen, SourceView, SymbolScreen, TrailApp
from git_trail.git import LineRange, Repository


async def settle(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


def content(app, selector):
    value = app.query_one(selector, Static).content
    return value.plain if hasattr(value, "plain") else str(value)


def displayed_patch(app):
    rendered = app.query_one("#diff", Static).content
    assert isinstance(rendered, Syntax)
    return rendered.code


async def test_initial_selection_loads_committed_source_timeline_and_diff(range_history):
    app = TrailApp(
        Repository(range_history.repo.root),
        InitialSelection(range_history.path, LineRange(4, 7)),
    )
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        assert app.snapshot is not None
        assert app.snapshot.commit == range_history.delete
        assert app.query_one("#source", SourceView).text == app.snapshot.text
        assert app.timeline is not None
        assert [entry.commit for entry in app.timeline.entries] == range_history.relevant
        assert app.query_one("#timeline", OptionList).option_count == 4
        assert "-    b = 2" in displayed_patch(app)
        assert range_history.delete[:10] in content(app, "#diff-title")
        assert "4 changes" in content(app, "#status")


async def test_keyboard_navigation_updates_diff_and_clamps_at_history_ends(range_history):
    app = TrailApp(
        Repository(range_history.repo.root),
        InitialSelection(range_history.path, LineRange(4, 7)),
    )
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        timeline = app.query_one("#timeline", OptionList)
        assert timeline.highlighted == 0
        await pilot.press("down")
        await pilot.pause()
        assert timeline.highlighted == 1
        assert "+    c = 3" in displayed_patch(app)
        await pilot.press("ctrl+j")
        await pilot.pause()
        assert timeline.highlighted == 2
        assert "-    a = 1" in displayed_patch(app)
        await pilot.press("ctrl+j", "ctrl+j", "ctrl+j")
        await pilot.pause()
        assert timeline.highlighted == 3
        assert "+def target():" in displayed_patch(app)
        await pilot.press("ctrl+k", "ctrl+k", "ctrl+k", "ctrl+k")
        await pilot.pause()
        assert timeline.highlighted == 0
        assert "-    b = 2" in displayed_patch(app)


async def test_history_shortcuts_work_while_source_is_focused(range_history):
    app = TrailApp(
        Repository(range_history.repo.root),
        InitialSelection(range_history.path, LineRange(4, 7)),
    )
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        source = app.query_one("#source", SourceView)
        source.focus()
        await pilot.pause()
        await pilot.press("ctrl+j")
        await pilot.pause()
        assert app.query_one("#timeline", OptionList).highlighted == 1
        await pilot.press("ctrl+k")
        await pilot.pause()
        assert app.query_one("#timeline", OptionList).highlighted == 0
        assert source.text == app.snapshot.text


async def test_filter_and_enter_load_a_file_then_enter_traces_cursor_line(range_history):
    app = TrailApp(Repository(range_history.repo.root))
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        assert app.snapshot is None
        file_filter = app.query_one("#file-filter", Input)
        assert app.focused is file_filter
        await pilot.press(*"player.py", "enter")
        await settle(app, pilot)
        assert app.filtered_files == (range_history.path,)
        assert app.snapshot.path == range_history.path
        source = app.query_one("#source", SourceView)
        assert app.focused is source
        source.move_cursor((4, 0))
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.timeline.selection.start == app.timeline.selection.end == 5
        assert [entry.commit for entry in app.timeline.entries] == [
            range_history.edit, range_history.origin,
        ]


async def test_shift_selection_excludes_endpoint_at_start_of_next_line(range_history):
    app = TrailApp(Repository(range_history.repo.root), InitialSelection(range_history.path))
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        source = app.query_one("#source", SourceView)
        source.move_cursor((3, 0))
        await pilot.press("shift+down", "shift+down", "shift+down", "shift+down")
        await pilot.pause()
        selected = app.selected_range()
        assert (selected.start, selected.end) == (4, 7)
        await pilot.press("enter")
        await settle(app, pilot)
        assert [entry.commit for entry in app.timeline.entries] == range_history.relevant


async def test_paragraph_and_containing_symbol_shortcuts_select_expected_range(range_history):
    app = TrailApp(Repository(range_history.repo.root), InitialSelection(range_history.path))
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        source = app.query_one("#source", SourceView)
        source.move_cursor((4, 2))
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert (app.selected_range().start, app.selected_range().end) == (4, 7)
        source.move_cursor((4, 2))
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert (app.selected_range().start, app.selected_range().end) == (4, 7)
        assert "function target" in content(app, "#status")
        source.move_cursor((7, 0))
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert "blank" in content(app, "#status").lower()


async def test_symbol_picker_and_jump_dialog_use_keyboard_and_validate_bounds(range_history):
    app = TrailApp(Repository(range_history.repo.root), InitialSelection(range_history.path))
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        await pilot.press("ctrl+b")
        await pilot.pause()
        assert isinstance(app.screen, SymbolScreen)
        await pilot.press("enter")
        await pilot.pause()
        assert not isinstance(app.screen, SymbolScreen)
        assert (app.selected_range().start, app.selected_range().end) == (4, 7)
        await pilot.press("ctrl+g")
        await pilot.pause()
        assert isinstance(app.screen, JumpScreen)
        await pilot.press("0", "enter")
        await pilot.pause()
        assert isinstance(app.screen, JumpScreen)
        assert "1 to 9" in content(app.screen, "#jump-message")
        jump = app.screen.query_one("#jump-input", Input)
        jump.value = "5"
        await pilot.press("enter")
        await pilot.pause()
        assert not isinstance(app.screen, JumpScreen)
        assert app.query_one("#source", SourceView).cursor_location == (4, 0)


async def test_missing_or_binary_file_clears_stale_source_and_timeline(range_history):
    repo = range_history.repo
    repo.write("binary.bin", b"binary\0fixture")
    repo.commit("Add binary fixture")
    app = TrailApp(
        Repository(repo.root),
        InitialSelection(range_history.path, LineRange(4, 7)),
    )
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        assert app.timeline is not None
        await app.load_file("binary.bin")
        await pilot.pause()
        assert app.snapshot is None
        assert app.timeline is None
        assert app.query_one("#source", SourceView).text == ""
        assert app.query_one("#timeline", OptionList).option_count == 0
        assert "binary" in content(app, "#status").lower()
        await app.load_file("not-present.py")
        await pilot.pause()
        assert app.snapshot is None
        assert app.timeline is None
        assert "not-present.py" in content(app, "#status")


async def test_dirty_working_tree_is_labeled_and_source_stays_read_only(range_history):
    repo = range_history.repo
    repo.write(range_history.path, "LOCAL REPLACEMENT\n")
    before = repo.state()
    app = TrailApp(
        Repository(repo.root),
        InitialSelection(range_history.path, LineRange(4, 7)),
    )
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        source = app.query_one("#source", SourceView)
        assert "LOCAL REPLACEMENT" not in source.text
        assert source.read_only
        assert "local edits excluded" in content(app, "#status").lower()
        source.focus()
        before_source = source.text
        await pilot.press("x", "backspace", "delete")
        await pilot.pause()
        assert source.text == before_source
    assert repo.state() == before


@pytest.mark.parametrize("separator", ["\u2028", "\v", "\r"])
async def test_source_display_rows_and_cursor_follow_git_lf_coordinates(repo, separator):
    repo.write("file.txt", f"one{separator}still one\nold selected\nlast\n")
    origin = repo.commit("LF rows")
    repo.write("file.txt", f"one{separator}still one\nnew selected\nlast\n")
    edit = repo.commit("Second LF row")
    app = TrailApp(Repository(repo.root), InitialSelection("file.txt"))
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        source = app.query_one("#source", SourceView)
        assert len(source.document.lines) == 4
        assert source.document.lines[1] == "new selected"
        source.move_cursor((1, 0))
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.timeline.selection.start == app.timeline.selection.end == 2
        assert [entry.commit for entry in app.timeline.entries] == [edit, origin]
        assert "-old selected" in displayed_patch(app)


async def test_timeline_displays_commit_date_instead_of_old_author_date(repo, monkeypatch):
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2020-02-02T12:30:00+05:30")
    repo.write("file.txt", "value\n")
    repo.commit("Applied later")
    app = TrailApp(Repository(repo.root), InitialSelection("file.txt", LineRange(1, 1)))
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        prompt = app.query_one("#timeline", OptionList).get_option_at_index(0).prompt
        text = prompt.plain if hasattr(prompt, "plain") else str(prompt)
        assert "2020-02-02" in text
        assert "2001-01-01" not in text
        assert "2020-02-02" in content(app, "#diff-title")


async def test_slow_previous_file_load_cannot_replace_newer_selection(repo, monkeypatch):
    repo.write("first.txt", "first source\n")
    repo.write("second.txt", "second source\n")
    repo.commit("Two files")
    repository = Repository(repo.root)
    original_snapshot = repository.snapshot
    started = threading.Event()
    release = threading.Event()

    def delayed_snapshot(path):
        if path == "first.txt":
            started.set()
            if not release.wait(timeout=10):
                raise RuntimeError("Test did not release delayed snapshot")
        return original_snapshot(path)

    monkeypatch.setattr(repository, "snapshot", delayed_snapshot)
    app = TrailApp(repository)
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        older_task = asyncio.create_task(app.load_file("first.txt"))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            await app.load_file("second.txt")
            release.set()
            await older_task
            await pilot.pause()
            assert app.snapshot.path == "second.txt"
            assert app.query_one("#source", SourceView).text == "second source\n"
            assert "second.txt" in content(app, "#source-title")
        finally:
            release.set()
            await older_task


async def test_slow_history_for_previous_file_cannot_overwrite_new_file(repo, monkeypatch):
    repo.write("first.txt", "first source\n")
    repo.write("second.txt", "second source\n")
    repo.commit("Two files")
    repository = Repository(repo.root)
    original_history = repository.history
    started = threading.Event()
    release = threading.Event()

    def delayed_history(snapshot, selection):
        if snapshot.path == "first.txt":
            started.set()
            if not release.wait(timeout=10):
                raise RuntimeError("Test did not release delayed history")
        return original_history(snapshot, selection)

    monkeypatch.setattr(repository, "history", delayed_history)
    app = TrailApp(repository, InitialSelection("first.txt"))
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        older_task = asyncio.create_task(app.trace_selection(LineRange(1, 1)))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            await app.load_file("second.txt")
            release.set()
            await older_task
            await pilot.pause()
            assert app.snapshot.path == "second.txt"
            assert app.timeline is None
            assert app.query_one("#timeline", OptionList).option_count == 0
            assert "first source" not in str(app.query_one("#diff", Static).content)
        finally:
            release.set()
            await older_task

from __future__ import annotations

import asyncio
import gc
import threading
import warnings
from dataclasses import dataclass, replace

import pytest
from rich.console import Console
from rich.syntax import Syntax
from rich.text import Text
from textual.widgets import Button, Input, Static

from git_trail.app import HelpScreen, InitialSelection, SourceView, TrailApp
from git_trail.git import LineRange, Repository, TrailError


@dataclass(frozen=True)
class TwoEdits:
    repo: object
    path: str
    origin: str
    edit: str
    old: str
    new: str


@pytest.fixture
def two_edits(repo):
    path = "src/module.py"
    old = (
        "def target():\n"
        "    # first value\n"
        "    first = 1\n"
        "\n"
        "    # stable middle\n"
        "    middle = 0\n"
        "    second = 2\n"
        "    return first + second + middle\n"
        "    # end\n"
    )
    repo.write(path, old)
    repo.write("other.txt", "other first\nother second\nother third\n")
    origin = repo.commit("Introduce two values")
    new = old.replace("first = 1", "first = 10").replace("second = 2", "second = 20")
    repo.write(path, new)
    edit = repo.commit("Edit separated values")
    return TwoEdits(repo, path, origin, edit, old, new)


async def settle(app, pilot):
    async def idle():
        for _ in range(10):
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            if not app.workers:
                return
        pytest.fail("Diff navigation did not reach an idle UI state.")

    await asyncio.wait_for(idle(), timeout=15)


async def focus(app, pilot, selector):
    widget = app.query_one(selector)
    widget.focus()
    await pilot.pause()
    assert app.focused is widget
    return widget


async def command(app, pilot, value):
    await pilot.press(":")
    field = app.query_one("#command-input", Input)
    assert app.focused is field
    field.value = value
    await pilot.press("enter")
    await settle(app, pilot)


def content(app, selector):
    value = app.query_one(selector, Static).content
    return value.plain if hasattr(value, "plain") else str(value)


def raw_index(app, text):
    return next(index for index, line in enumerate(app._diff_patch.lines) if line.text == text)


def scene(app):
    source = app.query_one("#source", SourceView)
    return (
        app.repository.commit, app.snapshot, app.traced_snapshot, app.timeline,
        app.selected_entry, app.source_mode, source.text, source.selection,
        tuple(entry.commit for entry in app.filtered_entries),
        app.query_one("#history-filter", Input).value,
        tuple(app.query_one(f"#view-{mode}", Button).has_class("active")
              for mode in ("before", "after", "starting")),
        content(app, "#workspace-title"), content(app, "#diff-title"),
    )


def assert_source(app, commit, path, line, text):
    assert (app.snapshot.commit, app.snapshot.path) == (commit, path)
    assert (app.selected_range().start, app.selected_range().end) == (line, line)
    assert app.snapshot.lines[line - 1] == text
    source = app.query_one("#source", SourceView)
    assert source.read_only
    assert text in source.selected_text
    assert app._navigation_ready and app.current_visit() is not None


async def click_diff(app, pilot, raw, side):
    row = next(row for row, item in enumerate(app._diff_rows) if raw in item.indices)
    pane = app.query_one("#diff-scroll")
    pane.scroll_to(y=max(0, row - 1), x=0, animate=False, immediate=True)
    await pilot.pause()
    x = 6 if side == "before" else app._diff_separator + 3
    assert x < pane.scrollable_content_region.width
    assert await pilot.click("#diff", offset=(x, row))
    await pilot.pause()
    assert app.focused is pane


def assert_highlight(app, raw):
    value = app.query_one("#diff", Static).content
    text = value.highlight(value.code) if isinstance(value, Syntax) else value
    assert isinstance(text, Text)
    position = text.plain.index(app._diff_patch.lines[raw].text)
    color = text.get_style_at_offset(Console(), position).bgcolor
    assert color is not None and color.triplet.hex == "#283457"


async def test_change_keys_and_unified_row_motions_jump_to_the_actual_old_or_new_line(two_edits):
    fixture = two_edits
    before = fixture.repo.state()
    app = TrailApp(Repository(fixture.repo.root), InitialSelection(fixture.path, LineRange(1, 9)))
    async with app.run_test(size=(160, 48)) as pilot:
        await settle(app, pilot)
        first = raw_index(app, "-    first = 1")
        second = raw_index(app, "-    second = 2")
        assert len(app._diff_patch.hunks) == 1
        assert app._diff_patch.change_targets() == (first, second)
        await focus(app, pilot, "#diff-scroll")
        for keys, expected in (
            (("]", "c"), first), (("]", "c"), second), (("]", "c"), first),
            (("[", "c"), second), (("f6",), first), (("shift+f6",), second),
        ):
            await pilot.press(*keys)
            assert app._diff_cursor == expected
            assert app._diff_active and app._diff_side == "before"
            assert_highlight(app, expected)
        await pilot.press("f6", "j")
        added = raw_index(app, "+    first = 10")
        assert app._diff_cursor == added and app._diff_side == "after"
        await pilot.press("enter")
        await settle(app, pilot)
        assert_source(app, fixture.edit, fixture.path, 3, "    first = 10")
        assert app.source_mode == "after"
        await focus(app, pilot, "#diff-scroll")
        await pilot.press("k")
        assert app._diff_cursor == first and app._diff_side == "before"
        await pilot.press("enter")
        await settle(app, pilot)
        assert_source(app, fixture.origin, fixture.path, 3, "    first = 1")
        await focus(app, pilot, "#diff-scroll")
        await pilot.press("down")
        assert app._diff_cursor == added and app._diff_side == "after"
        await pilot.press("up")
        assert app._diff_cursor == first and app._diff_side == "before"
        settled, visit = scene(app), app.current_visit()
        index, visits = app.journal.index, list(app.journal.visits)
        await pilot.press("g", "g")
        assert app._diff_cursor == 0
        await pilot.press("enter")
        await settle(app, pilot)
        assert "No source line" in content(app, "#status")
        assert scene(app) == settled and app.current_visit() == visit
        assert app.journal.index == index and app.journal.visits == visits
        await focus(app, pilot, "#diff-scroll")
        await pilot.press("G")
        assert app._diff_cursor == len(app._diff_patch.lines) - 1
        assert app._diff_patch.lines[app._diff_cursor].text == "     # end"
        await pilot.press("enter")
        await settle(app, pilot)
        assert_source(app, fixture.origin, fixture.path, 9, "    # end")
        await focus(app, pilot, "#diff-scroll")
        app.select_diff_index(raw_index(app, " "), "before")
        await pilot.press("enter")
        await settle(app, pilot)
        assert_source(app, fixture.origin, fixture.path, 4, "")
    assert fixture.repo.state() == before


async def test_colon_commands_open_exact_source_sides_and_keep_the_timeline_anchor(two_edits):
    fixture = two_edits
    before = fixture.repo.state()
    app = TrailApp(Repository(fixture.repo.root), InitialSelection(fixture.path, LineRange(1, 9)))
    async with app.run_test(size=(160, 48)) as pilot:
        await settle(app, pilot)
        timeline, anchor, entry = app.timeline, app.traced_snapshot, app.selected_entry
        first = raw_index(app, "-    first = 1")
        second = raw_index(app, "-    second = 2")
        await command(app, pilot, "nextchange")
        assert app._diff_active and app._diff_cursor == first
        assert app.focused is app.query_one("#diff-scroll")
        await command(app, pilot, "prevchange")
        assert app._diff_cursor == second
        await command(app, pilot, "nextchange")
        assert app._diff_cursor == first
        await command(app, pilot, "diffsource")
        assert_source(app, fixture.origin, fixture.path, 3, "    first = 1")
        await command(app, pilot, "split")
        await command(app, pilot, "diffafter")
        assert_source(app, fixture.edit, fixture.path, 3, "    first = 10")
        assert app.source_mode == "after"
        assert app.query_one("#view-after", Button).has_class("active")
        await command(app, pilot, "diffbefore")
        assert_source(app, fixture.origin, fixture.path, 3, "    first = 1")
        assert app.source_mode == "before"
        assert app.query_one("#view-before", Button).has_class("active")
        assert app.timeline is timeline and app.traced_snapshot is anchor and app.selected_entry is entry
        assert [item.commit for item in app.timeline.entries] == [fixture.edit, fixture.origin]
        assert (app.timeline.selection.start, app.timeline.selection.end) == (1, 9)
    assert fixture.repo.state() == before


async def test_split_mouse_cells_and_unified_resize_keep_the_same_raw_cursor_and_highlight(two_edits):
    fixture = two_edits
    before = fixture.repo.state()
    app = TrailApp(Repository(fixture.repo.root), InitialSelection(fixture.path, LineRange(1, 9)))
    async with app.run_test(size=(160, 48)) as pilot:
        await settle(app, pilot)
        await command(app, pilot, "split")
        removed, added = raw_index(app, "-    first = 1"), raw_index(app, "+    first = 10")
        row = next(item for item in app._diff_rows if removed in item.indices)
        assert row.old == removed and row.new == added
        await click_diff(app, pilot, removed, "before")
        assert app._diff_cursor == removed and app._diff_side == "before"
        await click_diff(app, pilot, removed, "after")
        assert app._diff_cursor == added and app._diff_side == "after"
        await pilot.press("enter")
        await settle(app, pilot)
        assert_source(app, fixture.edit, fixture.path, 3, "    first = 10")
        await click_diff(app, pilot, removed, "before")
        await pilot.press("enter")
        await settle(app, pilot)
        assert_source(app, fixture.origin, fixture.path, 3, "    first = 1")
        raw_patch, visit = app.selected_entry.patch, app.current_visit()
        await command(app, pilot, "unified")
        assert app._diff_cursor == removed and app._diff_side == "before"
        assert_highlight(app, removed)
        await command(app, pilot, "split")
        await pilot.resize_terminal(80, 24)
        await settle(app, pilot)
        assert app.diff_layout == "split" and app.diff_effective_layout == "unified"
        assert app._diff_cursor == removed and app._diff_side == "before"
        assert app._diff_patch.lines[removed].old == 3
        assert_highlight(app, removed)
        assert app.current_visit() == visit and app.selected_entry.patch == raw_patch
        await pilot.resize_terminal(160, 48)
        await settle(app, pilot)
        assert app.diff_effective_layout == "split"
        assert app._diff_cursor == removed and app._diff_side == "before"
        assert app.current_visit() == visit
        cursor = app._diff_cursor
        app.diff_click(0, 6)
        assert app._diff_cursor == cursor
    assert fixture.repo.state() == before


async def test_diff_shortcuts_preserve_prompt_modal_and_source_visual_input(two_edits):
    fixture = two_edits
    app = TrailApp(Repository(fixture.repo.root), InitialSelection(fixture.path, LineRange(1, 9)))
    async with app.run_test(size=(160, 48)) as pilot:
        await settle(app, pilot)
        source = await focus(app, pilot, "#source")
        original, cursor = scene(app), app._diff_cursor
        await pilot.press("ctrl+f")
        field = app.query_one("#find-input", Input)
        assert app.focused is field
        await pilot.press("]", "c", "j", "k", "g", "f6", "shift+f6")
        assert field.value == "]cjkg"
        assert app.focused is field and app._diff_cursor == cursor
        assert scene(app) == original
        await pilot.press("escape", "f1")
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("f6", "shift+f6", "]", "c", "j", "k")
        assert isinstance(app.screen, HelpScreen) and app._diff_cursor == cursor
        await pilot.press("escape")
        await focus(app, pilot, "#source")
        source.move_cursor((2, 0))
        await pilot.press("V")
        selected = source.selected_text
        assert content(app, "#mode") == "V-LINE"
        await pilot.press("f6", "shift+f6", "]", "c")
        assert source.selected_text == selected and content(app, "#mode") == "V-LINE"
        assert app.focused is source and app._diff_cursor == cursor
        await pilot.press("escape", "j")
        assert source.cursor_location[0] == 3
        assert app._diff_cursor == cursor


@pytest.mark.parametrize("failure_at", ["diff_source", "details"])
async def test_failed_and_rapidly_superseded_jumps_keep_source_journal_and_bookmark_readiness(two_edits, monkeypatch, failure_at):
    fixture = two_edits
    before = fixture.repo.state()
    app = TrailApp(Repository(fixture.repo.root), InitialSelection(fixture.path, LineRange(1, 9)))
    async with app.run_test(size=(160, 48)) as pilot:
        await settle(app, pilot)
        app.select_range(LineRange(2, 4))
        await command(app, pilot, "split")
        app.select_diff_index(raw_index(app, "-    first = 1"), "before")
        await pilot.pause()
        original, visit = scene(app), app.current_visit()
        index, visits = app.journal.index, list(app.journal.visits)

        def fail(*args, **kwargs):
            raise TrailError(f"Unavailable {failure_at} during source preparation")

        monkeypatch.setattr(app.repository, failure_at, fail)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", RuntimeWarning)
            app.jump_diff_source("before")
            app.jump_diff_source("after")
            app.jump_diff_source("before")
            await settle(app, pilot)
            gc.collect()
            await pilot.pause()
        assert not [item for item in caught if issubclass(item.category, RuntimeWarning)
                    and "never awaited" in str(item.message)]
        assert "Could not open diff source" in content(app, "#status")
        assert failure_at in content(app, "#status")
        assert scene(app) == original and app.current_visit() == visit
        assert app.journal.index == index and app.journal.visits == visits
        app.save_bookmark("Still available")
        assert app.saved_bookmarks()["Still available"] == visit.to_dict()
    assert fixture.repo.state() == before


@pytest.mark.parametrize(
    "newer",
    ["file", "filter", "view", "bookmark", "diff-cursor", "diff-side",
     "source-selection", "invalid-filter", "failed-bookmark"],
)
async def test_a_slow_diff_source_jump_cannot_overwrite_newer_navigation(two_edits, monkeypatch, newer):
    fixture = two_edits
    before = fixture.repo.state()
    app = TrailApp(Repository(fixture.repo.root), InitialSelection(fixture.path, LineRange(1, 9)))
    async with app.run_test(size=(160, 48)) as pilot:
        await settle(app, pilot)
        app.select_range(LineRange(2, 2))
        app.save_bookmark("After line two")
        saved = app.current_visit()
        app.select_range(LineRange(1, 9))
        await pilot.pause()
        settled, settled_visit = scene(app), app.current_visit()
        original = app.repository.diff_source
        started, released = threading.Event(), threading.Event()

        def delayed(*args, **kwargs):
            started.set()
            if not released.wait(timeout=10):
                raise RuntimeError("Test did not release the diff source lookup")
            return original(*args, **kwargs)

        monkeypatch.setattr(app.repository, "diff_source", delayed)
        target = raw_index(app, "     # first value" if newer == "diff-side" else "-    first = 1")
        app.select_diff_index(target, "before")
        app.jump_diff_source()
        try:
            assert await asyncio.to_thread(started.wait, 5)
            if newer == "file":
                await app.load_file("other.txt")
            elif newer == "filter":
                app.query_one("#history-filter", Input).value = f"hash:{fixture.origin[:10]}"
                await pilot.pause()
            elif newer == "view":
                app.set_source_mode("starting")
                await pilot.pause()
            elif newer == "bookmark":
                app.open_bookmark("After line two")
                await pilot.pause()
            elif newer == "diff-cursor":
                app.action_nextchange()
                await pilot.pause()
            elif newer == "diff-side":
                app.select_diff_index(target, "after")
                await pilot.pause()
            elif newer == "source-selection":
                await focus(app, pilot, "#source")
                app.select_range(LineRange(6, 6))
                await pilot.pause()
            elif newer == "invalid-filter":
                await pilot.press("ctrl+l")
                field = app.query_one("#history-filter", Input)
                assert app.focused is field
                field.value = "after:2024-02-30"
                await pilot.pause()
            else:
                app.navigation_data["bookmarks"][app.repository_key]["Missing commit"] = replace(
                    saved, commit="f" * 40,
                ).to_dict()
                app.open_bookmark("Missing commit")
                await pilot.pause()
            released.set()
            await settle(app, pilot)
            if newer == "file":
                assert app.snapshot.path == "other.txt" and app.timeline is None
                assert app.query_one("#source", SourceView).text == "other first\nother second\nother third\n"
            elif newer == "filter":
                assert [item.commit for item in app.filtered_entries] == [fixture.origin]
                assert app.selected_entry.commit == fixture.origin
                assert app.snapshot.commit == fixture.origin and app.source_mode == "after"
            elif newer == "view":
                assert app.snapshot.commit == fixture.edit and app.source_mode == "starting"
                assert app.query_one("#source", SourceView).text == fixture.new
                assert (app.selected_range().start, app.selected_range().end) == (1, 9)
            elif newer == "bookmark":
                assert app.current_visit() == saved
                assert_source(app, fixture.edit, fixture.path, 2, "    # first value")
            elif newer in ("diff-cursor", "diff-side"):
                assert scene(app) == settled and app.current_visit() == settled_visit
                if newer == "diff-cursor":
                    assert app._diff_cursor == raw_index(app, "-    second = 2")
                    assert app._diff_side == "before"
                else:
                    assert app._diff_cursor == target and app._diff_side == "after"
            elif newer == "source-selection":
                assert_source(app, fixture.edit, fixture.path, 6, "    middle = 0")
                assert app.focused is app.query_one("#source")
                assert app.timeline is settled[3] and app.selected_entry is settled[4]
            elif newer == "invalid-filter":
                assert app.snapshot == settled[1] and app.timeline is settled[3]
                assert app.selected_entry is settled[4] and app.current_visit() == settled_visit
                assert "YYYY-MM-DD" in content(app, "#status")
                assert app.query_one("#prompt-row").display
                assert app.focused is app.query_one("#history-filter", Input)
                app.save_bookmark("Source retained after invalid filter")
                assert app.saved_bookmarks()["Source retained after invalid filter"] == settled_visit.to_dict()
            else:
                assert scene(app) == settled and app.current_visit() == settled_visit
                assert "Could not restore" in content(app, "#status")
                app.save_bookmark("Source retained after failed bookmark")
                assert app.saved_bookmarks()["Source retained after failed bookmark"] == settled_visit.to_dict()
            assert app._navigation_ready and app.current_visit() is not None
            if newer != "source-selection":
                assert app.journal.visits[app.journal.index] == app.current_visit()
        finally:
            released.set()
            await settle(app, pilot)
    assert fixture.repo.state() == before


async def test_diff_one_line_bookmark_back_and_forward_restore_the_actual_displayed_source(two_edits):
    fixture = two_edits
    before = fixture.repo.state()
    app = TrailApp(Repository(fixture.repo.root), InitialSelection(fixture.path, LineRange(1, 9)))
    async with app.run_test(size=(160, 48)) as pilot:
        await settle(app, pilot)
        app.select_range(LineRange(4, 5))
        leaving = app.current_visit()
        await focus(app, pilot, "#diff-scroll")
        await pilot.press("f6", "enter")
        await settle(app, pilot)
        assert_source(app, fixture.origin, fixture.path, 3, "    first = 1")
        jumped = app.current_visit()
        assert (jumped.commit, jumped.path, jumped.start, jumped.end) == (fixture.edit, fixture.path, 1, 9)
        assert (jumped.display_commit, jumped.display_start, jumped.display_end) == (fixture.origin, 3, 3)
        await command(app, pilot, "back")
        assert app.current_visit() == leaving
        assert app.snapshot.commit == fixture.edit and app.source_mode == "after"
        assert (app.selected_range().start, app.selected_range().end) == (4, 5)
        await command(app, pilot, "forward")
        assert app.current_visit() == jumped
        assert_source(app, fixture.origin, fixture.path, 3, "    first = 1")
        await command(app, pilot, "bookmark Exact old line")
        await app.load_file("other.txt")
        await settle(app, pilot)
        await command(app, pilot, "open Exact old line")
        assert app.current_visit() == jumped
        assert_source(app, fixture.origin, fixture.path, 3, "    first = 1")
        assert app.selected_entry.commit == fixture.edit
        assert [item.commit for item in app.timeline.entries] == [fixture.edit, fixture.origin]
        assert app.query_one("#view-before", Button).has_class("active")
    assert fixture.repo.state() == before


async def test_empty_split_cells_reject_jumps_for_deleted_lines_and_root_introductions(range_history):
    fixture = range_history
    before = fixture.repo.state()
    app = TrailApp(Repository(fixture.repo.root), InitialSelection(fixture.path, LineRange(4, 7)))
    async with app.run_test(size=(160, 48)) as pilot:
        await settle(app, pilot)
        await command(app, pilot, "split")
        removed = raw_index(app, "-    b = 2")
        await click_diff(app, pilot, removed, "after")
        original, visit = scene(app), app.current_visit()
        index, visits = app.journal.index, list(app.journal.visits)
        await pilot.press("enter")
        await settle(app, pilot)
        assert "No source line" in content(app, "#status")
        assert scene(app) == original and app.current_visit() == visit
        assert app.journal.index == index and app.journal.visits == visits
        await click_diff(app, pilot, removed, "before")
        await pilot.press("enter")
        await settle(app, pilot)
        assert_source(app, fixture.insert, fixture.path, 6, "    b = 2")
        app.set_source_mode("after")
        await settle(app, pilot)
        app.show_entry(len(app.filtered_entries) - 1)
        await settle(app, pilot)
        assert app.selected_entry.commit == fixture.origin
        added = raw_index(app, "+def target():")
        await click_diff(app, pilot, added, "before")
        original, visit = scene(app), app.current_visit()
        index, visits = app.journal.index, list(app.journal.visits)
        await pilot.press("enter")
        await settle(app, pilot)
        assert scene(app) == original and app.current_visit() == visit
        assert app.journal.index == index and app.journal.visits == visits
        await click_diff(app, pilot, added, "after")
        await pilot.press("enter")
        await settle(app, pilot)
        assert_source(app, fixture.origin, fixture.path, 3, "def target():")
    assert fixture.repo.state() == before


async def test_context_only_symbol_move_jumps_to_exact_old_and_new_file_lines(repo):
    body = (
        "def target(values):\n"
        "    positive = [value for value in values if value > 0]\n"
        "    total = sum(positive)\n"
        "    count = len(positive)\n"
        "    average = total / max(count, 1)\n"
        "    return round(average, 2)\n"
    )
    repo.write("old.py", "# original heading\n\n" + body)
    origin = repo.commit("Original source")
    (repo.root / "old.py").unlink()
    repo.write("new.py", body)
    moved = repo.commit("Move unchanged function")
    before = repo.state()
    app = TrailApp(Repository(repo.root), InitialSelection("new.py", LineRange(1, 6), follow_symbol=True))
    async with app.run_test(size=(160, 48)) as pilot:
        await settle(app, pilot)
        assert app.timeline.mode == "symbol" and app.selected_entry.commit == moved
        targets = app._diff_patch.change_targets()
        assert len(targets) == 1
        target = app._diff_patch.lines[targets[0]]
        assert target.kind == "context" and (target.old, target.new) == (3, 1)
        assert not any(line.kind in ("delete", "add") for line in app._diff_patch.lines)
        await command(app, pilot, "split")
        await command(app, pilot, "nextchange")
        assert app._diff_cursor == targets[0]
        await command(app, pilot, "diffbefore")
        assert_source(app, origin, "old.py", 3, "def target(values):")
        await command(app, pilot, "diffafter")
        assert_source(app, moved, "new.py", 1, "def target(values):")
        assert app.traced_snapshot.path == "new.py" and app.traced_snapshot.commit == moved
        assert app.selected_entry.commit == moved and app.timeline.mode == "symbol"
    assert repo.state() == before
